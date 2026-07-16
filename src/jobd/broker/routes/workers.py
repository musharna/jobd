"""Worker surface: /gpu-holders, /heartbeat, /workers, /next-job.

Stage-3 split (backlog 2026-07-15): endpoint bodies are VERBATIM from
app.py's build_app — build_router unpacks BrokerDeps into the same local
names the closures always captured, so the move is byte-identical at the
body level and the whole suite passes unchanged.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from jobd.broker.constants import _LONGPOLL_RECHECK_S
from jobd.broker.context import BrokerDeps
from jobd.broker.events import _emit_event
from jobd.broker.jobinfo import _to_info
from jobd.broker.state import (
    _deps_satisfied_bulk,
    _emit_cascade_cancellations,
    _excluded_workers,
    _reconcile_worker_in_flight,
)
from jobd.db import Job, Worker
from jobd.matcher import WorkerSnapshot
from jobd.models import (
    JobInfo,
    JobState,
    NextJobQuery,
    WorkerHeartbeat,
    WorkerInfo,
)


def build_router(deps: BrokerDeps) -> APIRouter:
    router = APIRouter()
    SessionLocal = deps.session_local
    logs_dir = deps.logs_dir
    state = deps.state
    _wake_dispatchers = deps.wake_dispatchers
    _loop_holder = deps.loop_holder
    _wake_holder = deps.wake_holder

    @router.get("/gpu-holders")
    def gpu_holders(host: str | None = None):
        """Audit 2026-05-18 (runtime-zombies S5): dual-signal GPU-holder
        probe. NVML compute-apps unioned with `fuser -v /dev/nvidia*` so
        the desktop-inference_server failure mode (NVML returns [N/A], only
        fuser sees the process) is observable.

        Returns [{pid, gpu_id, mem_mb, source, known, job_id, worker}].
        source ∈ {nvml, fuser, both}. `known` is True when the PID appears
        in a worker's heartbeat-reported per-job PID inventory (#7);
        job_id/worker say which job claims it. Note: the probe is
        broker-local — it probes the BROKER host, which is almost never the
        GPU host. Useful primarily when running directly on a worker.

        PIDs are host-local, so pass `?host=<worker>` to consult only that
        worker's inventory. The default consults every online worker, which
        can mis-attribute on a numeric pid collision across hosts — fine on
        a broker+worker single host, caveat emptor on a fleet.
        """
        from jobd.gpu_holder_probe import probe_gpu_holders

        pid_owner: dict[int, tuple[int, str]] = {}
        with SessionLocal() as session:
            q = select(Worker).where(Worker.state == "online")
            if host is not None:
                q = q.where(Worker.host == host)
            for w in session.execute(q).scalars():
                try:
                    inventory = json.loads(w.in_flight_pids_json or "{}")
                except ValueError:
                    continue
                for jid, pids in inventory.items():
                    for p in pids:
                        pid_owner.setdefault(int(p), (int(jid), w.host))
        return [
            {
                "pid": h.pid,
                "gpu_id": h.gpu_id,
                "mem_mb": h.mem_mb,
                "source": h.source,
                "known": h.known,
                "job_id": pid_owner[h.pid][0] if h.pid in pid_owner else None,
                "worker": pid_owner[h.pid][1] if h.pid in pid_owner else None,
            }
            for h in probe_gpu_holders(known_pids=set(pid_owner))
        ]

    @router.post("/heartbeat")
    def heartbeat(hb: WorkerHeartbeat):
        with SessionLocal() as session:
            worker = session.execute(
                select(Worker).where(Worker.host == hb.host)
            ).scalar_one_or_none()
            is_first_heartbeat = worker is None
            now = datetime.now(UTC)
            if worker is None:
                worker = Worker(host=hb.host, last_heartbeat=now)
                session.add(worker)
            worker.host_aliases_json = json.dumps(hb.host_aliases)
            worker.last_heartbeat = now
            worker.free_vram_gb = hb.free_vram_gb
            worker.unregistered_vram_gb = hb.unregistered_vram_gb
            worker.free_ram_gb = hb.free_ram_gb
            worker.idle_cpus = hb.idle_cpus
            worker.arch = hb.arch
            worker.os = hb.os
            worker.gpu = hb.gpu
            worker.tags_json = json.dumps(hb.tags)
            worker.mount_roots_json = json.dumps(hb.mount_roots)
            worker.max_concurrent = hb.max_concurrent
            worker.running = hb.running
            # Assigned unconditionally, including None: a worker that stops
            # reporting a version IS an older worker, and pinning the last-known
            # value would make the fleet look newer than it is.
            worker.version = hb.version
            worker.state = "online"
            if hb.in_flight_pids is not None:
                worker.in_flight_pids_json = json.dumps(hb.in_flight_pids)
            orphan_records: list[tuple[int, str]] = []
            cascade_records: list[tuple[int, str, list[tuple[int, str]]]] = []
            requeued: list[int] = []
            cancelled_records: list[tuple[int, str, str]] = []
            resurrected: list[tuple[int, str]] = []
            restored: list[tuple[int, str]] = []
            if hb.in_flight_job_ids is not None:
                (
                    orphan_records,
                    cascade_records,
                    requeued,
                    cancelled_records,
                    resurrected,
                    restored,
                ) = _reconcile_worker_in_flight(session, hb.host, set(hb.in_flight_job_ids))
            session.commit()
        if is_first_heartbeat:
            _emit_event(
                logs_dir,
                "worker_registered",
                source="broker",
                host=hb.host,
                host_aliases=list(hb.host_aliases),
                tags=list(hb.tags),
                gpu=hb.gpu,
                arch=hb.arch,
                os=hb.os,
                mount_roots=list(hb.mount_roots),
                version=hb.version,
            )
        for jid, proj in orphan_records:
            _emit_event(
                logs_dir,
                "job_orphaned",
                source="broker",
                job_id=jid,
                project=proj,
                worker=hb.host,
                termination_reason="worker_restarted",
            )
        for parent_id, parent_state, cascaded in cascade_records:
            _emit_cascade_cancellations(logs_dir, cascaded, parent_id, parent_state)
        for jid, proj, prior_state in cancelled_records:
            _emit_event(
                logs_dir,
                "job_cancelled",
                source="broker",
                job_id=jid,
                project=proj,
                prior_state=prior_state,
                by="user",
                via="reconcile",
            )
        for jid, proj in resurrected:
            _emit_event(
                logs_dir,
                "job_resurrected",
                source="broker",
                job_id=jid,
                project=proj,
                worker=hb.host,
                prior_state="orphaned",
                note="worker reported it still in flight; the worker-is-gone premise was false",
            )
        for jid, proj in restored:
            _emit_event(
                logs_dir,
                "job_uncancelled",
                source="broker",
                job_id=jid,
                project=proj,
                by="resurrect",
                note="parent was resurrected; this child had been cascade-cancelled",
            )
        # Requeues make jobs dispatchable again; orphans and cancels are
        # failed-side terminals that can unblock any-exit dependents (and their
        # cascades). All three change what the matcher would pick — wake
        # long-pollers rather than waiting out the recheck backstop. So do
        # `restored` children, which go back to QUEUED and are dispatchable now.
        if requeued or orphan_records or cancelled_records or restored:
            _wake_dispatchers()
        return {"ok": True}

    @router.get("/workers", response_model=list[WorkerInfo])
    def list_workers():
        with SessionLocal() as session:
            workers = session.execute(select(Worker)).scalars().all()
            return [
                WorkerInfo(
                    host=w.host,
                    host_aliases=json.loads(w.host_aliases_json or "[]"),
                    last_heartbeat=w.last_heartbeat,
                    state=w.state,
                    free_vram_gb=w.free_vram_gb,
                    unregistered_vram_gb=w.unregistered_vram_gb,
                    free_ram_gb=w.free_ram_gb,
                    idle_cpus=w.idle_cpus,
                    arch=w.arch,
                    os=w.os,
                    gpu=w.gpu,
                    tags=json.loads(w.tags_json or "[]"),
                    mount_roots=json.loads(w.mount_roots_json or "[]"),
                    max_concurrent=w.max_concurrent if w.max_concurrent is not None else 1,
                    running=w.running if w.running is not None else 0,
                    version=w.version,
                )
                for w in workers
            ]

    @router.delete("/workers/{host}")
    def delete_worker(host: str):
        with SessionLocal() as session:
            worker = session.execute(select(Worker).where(Worker.host == host)).scalar_one_or_none()
            if worker is None:
                raise HTTPException(status_code=404, detail=f"no such worker: {host}")
            if worker.state == "online":
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"worker {host!r} is online — stop the worker process first, "
                        "or wait for the sweeper to mark it offline"
                    ),
                )
            session.delete(worker)
            session.commit()
            return {"ok": True, "deleted": host}

    @router.post("/next-job", response_model=JobInfo | None)
    async def next_job(q: NextJobQuery):
        from jobd.matcher import pick_next_job

        # Capture the running loop so sync-thread wake sites can reach it (the
        # lifespan sets it too; this covers callers that skip lifespan, e.g.
        # TestClient without a context manager).
        _loop_holder[0] = asyncio.get_running_loop()

        def _attempt() -> JobInfo | None:
            """One claim attempt against the live queue. Returns the claimed job
            or None (no match OR lost race — both mean 'nothing for you now')."""
            with SessionLocal() as session:
                # Order in SQL rather than sorting in Python. `pick_next_job` applies
                # the same (priority desc, submitted_at asc) order itself, so this is
                # not a behavior change — it just lets the index do the work, and it
                # makes the scan order deterministic for the fits-on-worker walk.
                all_queued = (
                    session.execute(
                        select(Job)
                        .where(Job.state == JobState.QUEUED)
                        .order_by(Job.priority.desc(), Job.submitted_at.asc())
                    )
                    .scalars()
                    .all()
                )
                # One SELECT for every parent of every queued job, instead of a
                # point-read per parent per job (N+1) on every attempt by every
                # parked worker. See _deps_satisfied_bulk on why this is *fresher*
                # than the loop it replaces, not just cheaper.
                satisfied_ids = _deps_satisfied_bulk(all_queued, session)
                queued: list[Job] = [j for j in all_queued if j.id in satisfied_ids]
                worker_row = session.execute(
                    select(Worker).where(Worker.host == q.host)
                ).scalar_one_or_none()
                aliases: list[str] = ["any", "any-gpu"] if q.free_vram_gb > 0 else ["any"]
                arch = q.arch
                os_ = q.os
                gpu = q.gpu or q.free_vram_gb > 0
                tags: list[str] = list(q.tags)
                mount_roots: list[str] = list(q.mount_roots)
                if worker_row is not None:
                    aliases = json.loads(worker_row.host_aliases_json)
                    arch = worker_row.arch
                    os_ = worker_row.os
                    gpu = worker_row.gpu
                    tags = json.loads(worker_row.tags_json)
                    mount_roots = json.loads(worker_row.mount_roots_json or "[]")
                # Filter out jobs whose cwd can't exist on this worker. Non-empty
                # mount_roots is the signal that the worker reported — older
                # workers that don't advertise roots get the old behavior (all
                # jobs eligible).
                if mount_roots:
                    queued = [j for j in queued if any(j.cwd.startswith(r) for r in mount_roots)]
                # Drop jobs this host already refused for a missing cwd (B): the
                # per-job exclusion set guarantees a refused job re-routes to a
                # host that has the path and never hot-loops back here.
                queued = [j for j in queued if q.host not in _excluded_workers(j)]
                w = WorkerSnapshot(
                    host=q.host,
                    host_aliases=aliases,
                    free_vram_gb=q.free_vram_gb,
                    unregistered_vram_gb=q.unregistered_vram_gb,
                    free_ram_gb=q.free_ram_gb,
                    idle_cpus=q.idle_cpus,
                    arch=arch,
                    os=os_,
                    gpu=gpu,
                    tags=tags,
                )
                pick = pick_next_job(queued, w)
                if pick is None:
                    from jobd.matcher import explain_skip

                    skips = explain_skip(queued, w)
                    skip_state: dict[tuple[int, str], str] = state["dispatch_skip_state"]
                    queued_by_id = {j.id: j for j in queued}
                    for job_id, reason in skips:
                        # Key on (job, worker), not job alone. The reason is
                        # computed against THIS worker, so the same job yields
                        # different reasons on different hosts — keyed by job_id
                        # only, each worker's answer overwrote the last, the
                        # "changed?" guard was true on every poll, and the event
                        # fired every time (see BrokerState.dispatch_skip_state).
                        key = (job_id, q.host)
                        if skip_state.get(key) != reason:
                            skip_state[key] = reason
                            _emit_event(
                                logs_dir,
                                "dispatch_skip",
                                source="broker",
                                job_id=job_id,
                                project=queued_by_id[job_id].project,
                                worker=q.host,
                                reason=reason,
                            )
                    # Drop dedup entries for jobs this worker can no longer see,
                    # so a job that ran or was cancelled doesn't leak memory and a
                    # resubmit gets a fresh slot. Scoped to THIS worker's keys:
                    # `queued` has already been filtered by mount_roots and this
                    # host's exclusion set, so a job that is merely invisible *here*
                    # is still queued elsewhere, and evicting other hosts' entries
                    # for it would make them re-emit on their next poll.
                    # pop, not del: concurrent /next-job attempts share this dict
                    # and another thread can remove the same key underneath us.
                    # list(skip_state), not skip_state: iterating the live dict
                    # while a concurrent attempt INSERTS raises "dictionary
                    # changed size during iteration" → that /next-job 500s
                    # (audit 2026-07-15 F6).
                    for stale in [
                        k for k in list(skip_state) if k[1] == q.host and k[0] not in queued_by_id
                    ]:
                        skip_state.pop(stale, None)
                    return None
                # Atomic claim: only one worker can transition queued -> assigned
                result = session.execute(
                    Job.__table__.update()  # type: ignore[attr-defined]  # SQLAlchemy: Table.update on __table__
                    .where(Job.id == pick.id, Job.state == JobState.QUEUED)
                    .values(
                        state=JobState.ASSIGNED,
                        worker=q.host,
                        started_at=datetime.now(UTC),
                        # A signal on a QUEUED row is always a leftover from a
                        # previous incarnation (cancel of a queued job goes
                        # straight to CANCELLED; preempt applies only to
                        # assigned/running). Clear it at claim so a fresh run
                        # can never inherit a stale cancel/preempt an unguarded
                        # writer stamped mid-requeue (audit 2026-07-05 A1).
                        signal=None,
                    )
                )
                session.commit()
                if result.rowcount == 0:
                    return None  # lost race; re-attempt / re-wait
                session.refresh(pick)
                # SQLite stores naive UTC; mirror sweep_once's naive-to-naive
                # comparison so the subtract can't mix aware and naive.
                queue_wait_s = (
                    datetime.now(UTC).replace(tzinfo=None) - pick.submitted_at
                ).total_seconds()
                _emit_event(
                    logs_dir,
                    "job_dispatched",
                    source="broker",
                    job_id=pick.id,
                    project=pick.project,
                    worker=q.host,
                    queue_wait_s=round(queue_wait_s, 2),
                )
                # The claiming worker needs the REAL env to run the job — this is
                # the one read surface that must not redact (all other _to_info
                # callers keep the default redact_env=True). See _redact_env.
                return _to_info(pick, redact_env=False)

        # Long-poll: hold the request up to q.wait_s, re-attempting whenever a
        # dispatcher wake fires (or every _LONGPOLL_RECHECK_S as a backstop for
        # any wake site we miss). wait_s=0 → single attempt, legacy behavior.
        # The blocking claim runs in a thread (asyncio's default executor, NOT
        # the anyio pool that serves the sync endpoints) and the wait suspends on
        # the loop, so a parked long-poll holds no threadpool token (M4).
        wait_s = max(0.0, float(q.wait_s or 0.0))
        deadline = time.monotonic() + wait_s
        while True:
            # Capture the wake event BEFORE the attempt so a wake that fires
            # during the attempt isn't lost (it sets this exact event).
            wake = _wake_holder[0]
            info = await asyncio.to_thread(_attempt)
            if info is not None:
                return info
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return None
            # recheck backstop elapsed with no wake — loop and re-attempt
            with suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=min(remaining, _LONGPOLL_RECHECK_S))

    return router
