"""FastAPI application factory for jobd.

All endpoints in this file for now; split into submodules when it grows past
~500 lines or gets distinct subsystems.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sse_starlette.sse import EventSourceResponse

from jobd import __version__
from jobd import events as _events
from jobd.arrays import index_subs, render_cmd, render_env, sweep_member_subs
from jobd.auth import install_tailnet_acl, require_token

# Broker internals split into jobd.broker.* (see that package's docstring). They
# are re-imported here so the endpoint closures in build_app() — and the tests
# that monkeypatch e.g. jobd.app._cas_state — resolve them from this module's
# namespace exactly as when they were defined inline.
from jobd.broker.constants import (
    _AUTO_PREEMPT_WARNING_PREFIX,
    _BLOCKED_WARNING_PREFIX,
    _FAILED_SIDE_TERMINAL,
    _LONGPOLL_RECHECK_S,
    _UNMATCHEABLE_WARNING_PREFIX,
    AUTO_PREEMPT_MIN_RUNTIME_SECONDS,  # noqa: F401  # re-exported: tests read jobd.app.AUTO_PREEMPT_MIN_RUNTIME_SECONDS
    DEAD_WORKER_SECONDS,
    IDEMPOTENT_RECLAIM_SECONDS,
    JOB_RETENTION_DAYS_DEFAULT,
    MAX_LOG_CHUNK_BYTES,
    NON_TERMINAL_STATES,
    OFFLINE_AFTER_SECONDS,
    QUEUE_BLOCKED_THRESHOLD_SECONDS,
    STALE_WORKER_THRESHOLD_S_DEFAULT,
    SWEEP_INTERVAL_SECONDS,
    UNMATCHEABLE_THRESHOLD_SECONDS,
    WALL_CLOCK_BACKSTOP_GRACE_SECONDS,
)
from jobd.broker.context import BrokerState
from jobd.broker.events import _emit_event, _parse_since
from jobd.broker.jobinfo import _build_eta_ctx, _to_info
from jobd.broker.projects import (
    _persist_projects,
    _projects_to_jsonable,
)
from jobd.broker.scheduling import (
    _build_snapshots,
    _find_nonpreemptible_blocker,
    _find_preemptible_candidate,
    _serialization_warning,
)
from jobd.broker.state import (
    _cas_state,
    _cascade_on_parent_terminal,
    _deps_satisfied,
    _emit_cascade_cancellations,
    _excluded_workers,
    _reconcile_worker_in_flight,
    _reject_stale_worker,
)
from jobd.config import (
    ProjectEntry,
    load_classifier_rules,
    load_profiles,
    load_projects,
    resolve_priority,
    resolve_profile,
    resolve_project_defaults,
)
from jobd.db import Job, Worker, init_db, migrate
from jobd.matcher import (
    WorkerSnapshot,
    cwd_routability,
    eligible_workers,
    gpu_contention_warning,
    selectors_only_match,
    submit_preflight,
)
from jobd.metrics import build_metrics_app
from jobd.models import (
    TERMINAL_STATES,
    AdmissionRefusal,
    ClassifyRequest,
    ClassifyResult,
    EventIngest,
    FieldResolution,
    JobInfo,
    JobRequires,
    JobState,
    JobSubmit,
    NextJobQuery,
    ResolutionSource,
    ResolvedConfig,
    WorkerHeartbeat,
    WorkerInfo,
)

log = logging.getLogger("jobd")


def build_app(
    db_url: str,
    projects_path: Path | str,
    profiles_path: Path | str,
    classifier_path: Path | str,
    logs_path: Path | str | None = None,
) -> FastAPI:
    # Long-poll plumbing (see _wake_dispatchers below). Defined up here so both
    # lifespan and the sweep can reach the loop handle.
    _loop_holder: list[asyncio.AbstractEventLoop | None] = [None]
    _wake_holder: list[asyncio.Event] = [asyncio.Event()]

    async def _sweep_loop():
        while True:
            try:
                # Run the (blocking, SQLite-touching) sweep off the event loop so
                # it can't freeze every async endpoint for the sweep's duration —
                # incl. the up-to-5s busy_timeout stalls (M5).
                await asyncio.to_thread(_sweep_once)
            except Exception as e:
                log.warning("sweeper error: %s", e)
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)

    @asynccontextmanager
    async def lifespan(app):
        _loop_holder[0] = asyncio.get_running_loop()
        task = asyncio.create_task(_sweep_loop())
        yield
        task.cancel()

    app = FastAPI(
        title="jobd",
        version=__version__,
        lifespan=lifespan,
        dependencies=[Depends(require_token)],
    )
    install_tailnet_acl(app)

    engine = create_engine(db_url, future=True)

    # SQLite tuning: WAL lets the broker's readers (CLI/MCP polls, ETA queries)
    # proceed concurrently with worker writes instead of blocking on a single
    # global lock; busy_timeout makes a contended write wait up to 5s rather
    # than raising SQLITE_BUSY immediately; synchronous=NORMAL is the safe WAL
    # companion (durable across app crashes, only at risk on OS/power loss).
    # Applied per-connection (idempotent) and only for SQLite backends.
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _conn_record):  # pragma: no cover - driver cb
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

    init_db(engine)
    migrate(engine)
    SessionLocal = sessionmaker(engine, expire_on_commit=False)

    logs_dir = Path(logs_path) if logs_path else Path(os.environ.get("JOBD_LOGS_DIR", "./logs"))
    logs_dir.mkdir(parents=True, exist_ok=True)

    state: BrokerState = {
        "projects": load_projects(projects_path),
        "profiles": load_profiles(profiles_path),
        "classifier": load_classifier_rules(classifier_path),
        "paths": {
            "projects": Path(projects_path),
            "profiles": Path(profiles_path),
            "classifier": Path(classifier_path),
        },
        "logs_dir": logs_dir,
        # Per-(job_id) dedup map for dispatch_skip emission. Keyed by job_id,
        # value is the most-recently-emitted reason string. Reset on broker
        # restart (best-effort observability — see Task 4 brief).
        "dispatch_skip_state": {},
    }
    app.state.shared = state
    app.state.SessionLocal = SessionLocal

    # Prometheus metrics: mounted as a sub-app so it bypasses the global bearer
    # token (mounts don't inherit router dependencies). Its /metrics path is
    # exempted from the tailnet ACL in auth.py so an in-cluster Prometheus can
    # scrape it. Exposes only aggregate job/worker counts.
    app.mount("/metrics", build_metrics_app(SessionLocal))

    # Server-side long-poll wake. /next-job (async) with wait_s>0 suspends on an
    # asyncio.Event until a mutation that could newly satisfy a match (submit /
    # terminal transition / requeue) or a bounded recheck elapses — WITHOUT
    # holding an anyio threadpool thread, so a fleet of long-pollers can't starve
    # /heartbeat and the other sync endpoints (M4). We wake liberally; spurious
    # wakes are harmless (the waiter just re-runs the pick and re-waits), and
    # _LONGPOLL_RECHECK_S backstops any wake site we miss.
    #
    # Wakes originate on sync threadpool threads (submit/complete/cancel/…) and
    # the sweep thread, so they're marshalled onto the loop via
    # call_soon_threadsafe. The event is SWAPPED (not just set) on each wake: a
    # waiter captures the current event before its claim attempt, so a wake that
    # fires during the attempt lands on the captured (now-set) event and the
    # waiter returns immediately instead of missing it.
    def _wake_dispatchers() -> None:
        loop = _loop_holder[0]
        if loop is None:
            return  # no async waiter has run yet — nothing is parked

        def _fire() -> None:
            ev = _wake_holder[0]
            _wake_holder[0] = asyncio.Event()
            ev.set()

        with suppress(RuntimeError):  # loop closed during shutdown — nothing to wake
            loop.call_soon_threadsafe(_fire)

    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/gpu-holders")
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

    # response_model=None: dry-run returns a plan dict, live returns JobInfo.
    # Both paths are JSON-serializable; FastAPI dispatches per-response.
    @app.post("/submit", response_model=None)
    def submit(req: JobSubmit):
        profile_spec = None
        if req.profile:
            profile_spec = resolve_profile(state["profiles"], req.profile)
            if profile_spec is None:
                raise HTTPException(status_code=404, detail=f"unknown profile: {req.profile}")

        priority = resolve_priority(state["projects"], req.project, req.priority_delta)
        # Per docs/projects-yaml.md §3, resolution order is:
        # CLI > project_default > profile > global. The `req.<field>` here is
        # the CLI-side value; project defaults sit just below.
        proj_defaults = resolve_project_defaults(state["projects"], req.project)

        host_pin = req.host_pin
        if host_pin == "any" and proj_defaults.host_pin:
            host_pin = proj_defaults.host_pin
        elif host_pin == "any" and profile_spec and profile_spec.host_hint:
            host_pin = profile_spec.host_hint

        # Explicit `req.vram_gb > 0` wins; otherwise fall through to the
        # profile spec; otherwise 0 (matcher applies tier-tag / implicit-
        # floor fallback). See JobSubmit.vram_gb docstring + matcher's
        # `effective_vram_request_gb`.
        vram_gb = req.vram_gb if req.vram_gb > 0 else (profile_spec.vram_gb if profile_spec else 0)
        ram_gb = profile_spec.ram_gb if profile_spec else 0
        cpus = profile_spec.cpus if profile_spec else 1

        preemptible = req.preemptible
        if preemptible is None:
            preemptible = proj_defaults.preemptible
        if preemptible is None and profile_spec is not None:
            preemptible = profile_spec.preemptible
        if preemptible is None:
            preemptible = False

        if req.fast_path is not None:
            fast_path = req.fast_path
        else:
            fast_path = profile_spec.fast_path if profile_spec else False

        requires = req.requires
        if requires is None:
            requires = proj_defaults.requires
        if requires is None and profile_spec and profile_spec.requires:
            requires = profile_spec.requires
        requires_json = requires.model_dump_json() if requires is not None else "{}"

        max_wall_s = req.max_wall_s
        if max_wall_s is None:
            max_wall_s = proj_defaults.max_wall_s

        idle_timeout_s = req.idle_timeout_s
        if idle_timeout_s is None:
            idle_timeout_s = proj_defaults.idle_timeout_s

        checkpoint_grace_s = req.checkpoint_grace_s
        if checkpoint_grace_s is None:
            checkpoint_grace_s = proj_defaults.checkpoint_grace_s

        # Unknown-project warning: surface (don't refuse) so first-time
        # `--project new-experiment` keeps working with global defaults.
        unknown_project_warning: str | None = None
        if req.project not in state["projects"] and "_default" in state["projects"]:
            unknown_project_warning = (
                f"project {req.project!r} has no entry in projects.yaml; using global defaults"
            )

        # cwd sanity: Windows-mount paths only exist on the laptop (WSL). If
        # someone submits --cwd /mnt/c/... without pinning the laptop, the
        # worker will fail cd and every process will rc=127. Root cause of the
        # 2026-04-22 project-b storm.
        if req.cwd.startswith("/mnt/c/") and host_pin not in ("laptop", "MSI", "any-laptop"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"cwd {req.cwd!r} is under /mnt/c/ (Windows mount, laptop-only) "
                    f"but host_pin={host_pin!r}. Pass --host laptop, or stage data "
                    f"under a cross-host path like /tmp or a project-scoped dir."
                ),
            )

        with SessionLocal() as session:
            for dep_id in req.depends_on:
                parent = session.get(Job, dep_id)
                if parent is None:
                    raise HTTPException(
                        status_code=400, detail=f"depends_on refers to missing job: {dep_id}"
                    )
                # A default-policy (non-any-exit) child needs the parent to
                # reach COMPLETED. If the parent is ALREADY in a failed-side
                # terminal, it never will — and no future transition fires the
                # cascade to cancel this child, so it would strand in QUEUED
                # forever. Reject at submit instead. (any-exit children are fine:
                # any terminal parent satisfies their dep.)
                if not req.depends_on_any_exit and parent.state in _FAILED_SIDE_TERMINAL:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"depends_on parent {dep_id} is already {parent.state} "
                            "(a failed-side terminal); a default-policy dependent would "
                            "never dispatch. Resubmit the parent, or pass "
                            "depends_on_any_exit to proceed on any terminal state."
                        ),
                    )
            now = datetime.now(UTC)
            # Warnings are routing-derived (requires / host_pin / live capacity),
            # so a job array's members share one warning string — the per-member
            # `{i}` substitution only changes the command, not where it routes.
            online = list(
                session.execute(select(Worker).where(Worker.state == "online")).scalars().all()
            )
            snapshots = _build_snapshots(online)
            all_workers = list(session.execute(select(Worker)).scalars().all())
            all_snapshots = _build_snapshots(all_workers)
            ser_warn = _serialization_warning(requires, host_pin, snapshots, session)
            gpu_warn = gpu_contention_warning(requires, host_pin, snapshots)
            preflight_warn = submit_preflight(requires, host_pin, all_snapshots)
            cwd_route = cwd_routability(req.cwd, host_pin, all_snapshots)
            cwd_route_warn: str | None = None
            if cwd_route is not None:
                is_hard, cwd_msg = cwd_route
                if is_hard:
                    raise HTTPException(status_code=400, detail=cwd_msg)
                cwd_route_warn = cwd_msg
            warnings = [
                w
                for w in (
                    unknown_project_warning,
                    preflight_warn,
                    cwd_route_warn,
                    ser_warn,
                    gpu_warn,
                )
                if w is not None
            ]
            warning_text = "; ".join(warnings) if warnings else None

            # Resolve the per-member substitutions up front so dry-run reports
            # the same member count the live submit would create. Three forms,
            # mutually exclusive (sweep+count rejected at the model):
            #   sweep   → cartesian product of named axes, each carrying {i}
            #   count>1 → bare {i} = 0..count-1
            #   neither → a single ordinary job (NULL array columns)
            if req.sweep:
                member_subs = sweep_member_subs([(ax.key, ax.values) for ax in req.sweep])
            elif req.count > 1:
                member_subs = [index_subs(i) for i in range(req.count)]
            else:
                member_subs = None
            is_array = member_subs is not None
            subs_list = member_subs if member_subs is not None else [{}]
            member_count = len(subs_list)

            # Dry-run bail-out:
            # full validation + routing-decision has run above (profile lookup,
            # project defaults, cwd sanity, depends_on existence, preflight,
            # gpu_contention). Return the would-be plan WITHOUT inserting any
            # Job row or emitting `job_submitted`. One plan covers every array
            # member (routing ignores the per-member substitution); array_count
            # tells the caller how many members the live submit would create.
            if req.dry_run:
                eligible = eligible_workers(requires, host_pin, snapshots)
                # would_route_to: hostnames the matcher would currently
                # consider (capability + host_pin match, ignores load). Empty
                # list = unmatcheable right now (preflight_warn explains why).
                would_route_to = [w.host for w in eligible]
                # would_use_worker: pre-load tie-break is matcher-internal; at
                # preview time we surface "any of these candidates" rather
                # than promising a specific host. None when no candidates.
                would_use_worker = eligible[0].host if len(eligible) == 1 else None
                return {
                    "state": "dry-run",
                    "would_route_to": would_route_to,
                    "would_use_worker": would_use_worker,
                    "array_count": member_count,
                    "validation": {
                        "effective_priority": priority,
                        "effective_host_pin": host_pin,
                        "effective_preemptible": preemptible,
                        "effective_vram_gb": vram_gb,
                        "effective_ram_gb": ram_gb,
                        "effective_cpus": cpus,
                        "effective_max_wall_s": max_wall_s,
                        "effective_idle_timeout_s": idle_timeout_s,
                        "effective_checkpoint_grace_s": checkpoint_grace_s,
                        "effective_scheduling_timeout_s": req.scheduling_timeout_s,
                        "effective_fast_path": fast_path,
                        "effective_requires": (
                            requires.model_dump() if requires is not None else None
                        ),
                        "warnings": warnings,
                    },
                }

            # Fan out into members using the substitutions resolved above. A
            # non-array submit (member_subs is None) is one ordinary job with
            # NULL array columns and an unchanged single-JobInfo response.
            members: list[Job] = []
            for i, subs in enumerate(subs_list):
                member_cmd = render_cmd(req.cmd, subs) if is_array else req.cmd
                member_env = render_env(req.env, subs) if is_array else req.env
                job = Job(
                    project=req.project,
                    profile=req.profile,
                    host_pin=host_pin,
                    priority=priority,
                    state=JobState.QUEUED,
                    cmd_json=json.dumps(member_cmd),
                    cwd=req.cwd,
                    env_json=json.dumps(member_env),
                    preemptible=preemptible,
                    vram_gb=vram_gb,
                    ram_gb=ram_gb,
                    cpus=cpus,
                    session_id=req.session_id,
                    submitted_at=now,
                    requires_json=requires_json,
                    depends_on_json=json.dumps(req.depends_on),
                    depends_on_any_exit=req.depends_on_any_exit,
                    fast_path=fast_path,
                    max_wall_s=max_wall_s,
                    idle_timeout_s=idle_timeout_s,
                    checkpoint_grace_s=checkpoint_grace_s,
                    scheduling_timeout_s=req.scheduling_timeout_s,
                    submitted_via=req.submitted_via,
                    array_index=i if is_array else None,
                    array_size=member_count if is_array else None,
                )
                if warning_text:
                    job.warning = warning_text
                    job.warning_at = now
                session.add(job)
                members.append(job)

            # Flush to assign ids; the array shares the first member's id so
            # `job status A<id>` resolves without a separate id sequence.
            session.flush()
            member_ids = [j.id for j in members]
            array_id = member_ids[0] if is_array else None
            if is_array:
                for j in members:
                    j.array_id = array_id
            session.commit()

            # Emit events from captured scalars (no post-commit ORM reload per
            # member — every member shares project/priority/host_pin/warning).
            for jid in member_ids:
                _emit_event(
                    logs_dir,
                    "job_submitted",
                    source="broker",
                    job_id=jid,
                    project=req.project,
                    priority=priority,
                    host_pin=host_pin,
                    preemptible=preemptible,
                    warning=warning_text,
                )
                if warning_text:
                    _emit_event(
                        logs_dir,
                        "submit_warning",
                        source="broker",
                        job_id=jid,
                        project=req.project,
                        warning_text=warning_text,
                    )

            # New queued job(s) — wake any worker long-polling /next-job.
            _wake_dispatchers()
            if is_array:
                return {
                    "array_id": array_id,
                    "count": member_count,
                    "job_ids": member_ids,
                    "warnings": warnings,
                }
            eta_ctx = _build_eta_ctx(session)
            return _to_info(members[0], eta_ctx)

    @app.get("/jobs", response_model=list[JobInfo])
    def list_jobs(
        state_filter: str | None = None,
        project: str | None = None,
        warnings_only: bool = False,
        array_id: int | None = None,
    ):
        with SessionLocal() as session:
            stmt = select(Job).order_by(Job.id.desc())
            if state_filter:
                stmt = stmt.where(Job.state == state_filter)
            if project:
                stmt = stmt.where(Job.project == project)
            if warnings_only:
                stmt = stmt.where(Job.warning.is_not(None))
            if array_id is not None:
                # array members are ordered by index for readable `--array` output
                stmt = stmt.where(Job.array_id == array_id).order_by(None).order_by(Job.array_index)
            jobs = session.execute(stmt).scalars().all()
            eta_ctx = _build_eta_ctx(session)
            return [_to_info(j, eta_ctx) for j in jobs]

    @app.get("/jobs/{job_id}", response_model=JobInfo)
    def get_job(job_id: int):
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            eta_ctx = _build_eta_ctx(session)
            return _to_info(job, eta_ctx)

    @app.post("/jobs/{job_id}/log")
    async def append_log(
        job_id: int,
        request: Request,
        x_jobd_worker: str | None = Header(default=None),
    ):
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            # Drop log chunks from a stale worker (M2): a partition-reclaimed job
            # re-dispatched elsewhere must not have the original worker's output
            # interleaved into the new run's log file.
            _reject_stale_worker(job.worker, x_jobd_worker, job_id, "log append")
        body = await request.body()
        if len(body) > MAX_LOG_CHUNK_BYTES:
            raise HTTPException(status_code=413, detail="log chunk too large")
        log_file = logs_dir / f"{job_id}.log"
        with log_file.open("ab") as f:
            f.write(body)
        return {"bytes": len(body)}

    @app.post("/jobs/{job_id}/started", response_model=JobInfo)
    def started_job(job_id: int, x_jobd_worker: str | None = Header(default=None)):
        """Worker reports subprocess.Popen returned: assigned -> running.

        The matcher flips queued->assigned at dispatch and stamps started_at,
        but the actual subprocess launch happens later in the worker (systemd-run
        scope setup can take ~300ms). This endpoint marks the live transition
        so callers see `running` while the job actually executes, and the
        cancel signal_sent synthesis in MCP can tell "queued cancel" from
        "running SIGTERM" cleanly.

        Idempotent: only flips assigned->running. Terminal or already-running
        states are no-ops (the worker may retry on transient network errors).
        """
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            _reject_stale_worker(job.worker, x_jobd_worker, job_id, "started")
            # Guarded assigned->running: win only if the row is still ASSIGNED,
            # so a /started that races a concurrent sweeper orphan (or a cancel
            # that already went terminal) can't re-open a job that has left
            # ASSIGNED. Terminal / already-running states remain no-ops.
            won = _cas_state(session, job_id, (JobState.ASSIGNED,), state=JobState.RUNNING)
            if won:
                session.commit()
                session.refresh(job)
                _emit_event(
                    logs_dir,
                    "job_started",
                    source="broker",
                    job_id=job_id,
                    project=job.project,
                    worker=job.worker,
                )
            else:
                session.refresh(job)
            return _to_info(job)

    @app.post("/jobs/{job_id}/complete", response_model=JobInfo)
    def complete_job(job_id: int, payload: dict, x_jobd_worker: str | None = Header(default=None)):
        exit_code = payload.get("exit_code")
        final_state = payload.get("final_state")
        termination_reason = payload.get("termination_reason")
        if final_state is None:
            # Worker didn't specify — derive from exit code. This keeps the
            # depends_on cascade honest: nonzero rc must land in a failed-side
            # terminal so children that didn't opt into any_exit get cancelled.
            final_state = "completed" if exit_code in (0, None) else "failed"
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            # Refuse a terminal report from a stale worker (M2): a
            # partition-reclaimed job re-dispatched to a fresh worker must not be
            # terminal-ized (or have its outcome overwritten) by the original
            # worker that kept running. Checked before the CAS so the stale
            # /complete never wins the transition.
            _reject_stale_worker(job.worker, x_jobd_worker, job_id, "completion")
            # final_state must name a valid terminal state. Reject garbage so a
            # typo'd worker payload can't strand the job in a non-enum state that
            # later raises ValueError in JobState(job.state) on every read.
            try:
                final = JobState(final_state)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"invalid final_state {final_state!r}; must be one of "
                        f"{sorted(s.value for s in TERMINAL_STATES)}"
                    ),
                ) from None
            if final not in TERMINAL_STATES:
                raise HTTPException(
                    status_code=400,
                    detail=f"final_state {final_state!r} is not a terminal state",
                )
            # Terminal is terminal: a job already in a terminal state ignores a
            # late or duplicate /complete (worker retry, or the user cancelled /
            # the sweeper orphaned it first). Idempotent no-op — don't overwrite
            # the recorded outcome, don't re-cascade to dependents, don't re-emit
            # job_completed. The guard is a compare-and-swap (win only from a
            # non-terminal state) rather than a read-then-check so it can't race
            # a concurrent sweeper orphan: read-check-write let a late /complete
            # clobber a just-set ORPHANED back to COMPLETED (and vice versa).
            complete_values: dict = dict(
                state=final_state,
                exit_code=exit_code,
                finished_at=datetime.now(UTC),
                # Clear any pending signal — it's been honored (or irrelevant now
                # that the job is terminal). Otherwise a future reclaim/retry
                # would see a stale cancel signal and die on startup.
                signal=None,
            )
            if termination_reason is not None:
                complete_values["termination_reason"] = termination_reason
            won = _cas_state(session, job_id, NON_TERMINAL_STATES, **complete_values)
            if not won:
                session.refresh(job)
                return _to_info(job)
            session.refresh(job)
            cascaded = _cascade_on_parent_terminal(session, job, logs_dir)
            session.commit()
            session.refresh(job)
            wall_s = None
            if job.started_at is not None and job.finished_at is not None:
                wall_s = round((job.finished_at - job.started_at).total_seconds(), 2)
            _emit_event(
                logs_dir,
                "job_completed",
                source="broker",
                job_id=job_id,
                project=job.project,
                final_state=job.state,
                exit_code=job.exit_code,
                wall_s=wall_s,
                termination_reason=job.termination_reason,
                worker=job.worker,
            )
            _emit_cascade_cancellations(logs_dir, cascaded, job_id, job.state)
            # Terminal transition frees a slot and may unblock dependents — wake.
            _wake_dispatchers()
            return _to_info(job)

    @app.post("/jobs/{job_id}/cancel", response_model=JobInfo)
    def cancel_job(job_id: int):
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            cancel_event_kw: dict | None = None
            cascaded: list[tuple[int, str]] = []
            # Guarded queued->cancelled. Only wins if the row is STILL queued:
            # a worker's atomic queued->assigned claim can land between our read
            # and this write, and without the WHERE state=queued guard we'd
            # clobber that claim to CANCELLED without setting `signal` — the
            # worker would then run the job to completion while the user is told
            # it was cancelled (the H3 cancel-vs-claim TOCTOU).
            if job.state == JobState.QUEUED:
                won = _cas_state(
                    session,
                    job_id,
                    (JobState.QUEUED,),
                    state=JobState.CANCELLED,
                    finished_at=datetime.now(UTC),
                )
                if won:
                    session.refresh(job)  # sync ORM state for the cascade below
                    cascaded = _cascade_on_parent_terminal(session, job, logs_dir)
                    cancel_event_kw = dict(prior_state=JobState.QUEUED.value, by="user")
                else:
                    # Lost the race to the claim — re-read and fall through to
                    # the running/assigned signal path below.
                    session.refresh(job)
            if cancel_event_kw is None and job.state in (JobState.RUNNING, JobState.ASSIGNED):
                # Signal-based cancel: the worker honors it on its next poll.
                # Guarded so a job that reached a terminal state between our read
                # and here doesn't get a stale 'cancel' signal stamped on it.
                prior_state = job.state.value if hasattr(job.state, "value") else job.state
                won = _cas_state(
                    session,
                    job_id,
                    (JobState.RUNNING, JobState.ASSIGNED),
                    signal="cancel",
                )
                if won:
                    cancel_event_kw = dict(prior_state=prior_state, by="user_signal_pending")
            # else: terminal-state cancel is a no-op; don't emit
            session.commit()
            session.refresh(job)
            if cancel_event_kw is not None:
                _emit_event(
                    logs_dir,
                    "job_cancelled",
                    source="broker",
                    job_id=job_id,
                    project=job.project,
                    **cancel_event_kw,
                )
                _emit_cascade_cancellations(logs_dir, cascaded, job_id, JobState.CANCELLED.value)
                # A cancel can unblock depends_on_any_exit dependents — wake.
                _wake_dispatchers()
            return _to_info(job)

    @app.post("/jobs/{job_id}/preempt", response_model=JobInfo)
    def preempt_job(job_id: int):
        """Preempt a running/assigned job. Worker SIGTERMs the child with
        the same grace flow as cancel; final state will be 'preempted'.
        Refuses if job is not running/assigned (409) or not preemptible (409)."""
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            if job.state not in (JobState.RUNNING, JobState.ASSIGNED):
                raise HTTPException(
                    status_code=409,
                    detail=f"job {job_id} is {job.state} — preempt only applies to running/assigned jobs",
                )
            if not job.preemptible:
                raise HTTPException(
                    status_code=409,
                    detail=f"job {job_id} is not preemptible (submit with --preemptible or set project default)",
                )
            # Guarded so a job that reached a terminal state between the check
            # above and this write doesn't get a stale 'preempt' signal stamped
            # on it (which a later reclaim/retry would honor and mis-kill).
            _cas_state(session, job_id, (JobState.RUNNING, JobState.ASSIGNED), signal="preempt")
            session.commit()
            session.refresh(job)
            return _to_info(job)

    @app.post("/jobs/{job_id}/preempt-blockers")
    def preempt_blockers(job_id: int, force: bool = False):
        """Manual escalation: find a preemptible blocker for queued job
        `job_id` on its eligible workers and signal it now (skipping the
        sweeper's queue-age and runtime guards). With `force=true`, also
        drop the priority guard (preempt blockers at equal-or-higher
        priority — operator override).

        Refuses (409) if the job isn't queued; returns
        `{"signaled": <blocker_id>}` on success or `{"signaled": null,
        "reason": ...}` if no candidate qualifies.
        """
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            if job.state != JobState.QUEUED:
                raise HTTPException(
                    status_code=409,
                    detail=f"job {job_id} is {job.state} — preempt-blockers only applies to queued jobs",
                )
            req: JobRequires | None = None
            if job.requires_json and job.requires_json != "{}":
                try:
                    req = JobRequires.model_validate_json(job.requires_json)
                except Exception:
                    req = None
            workers = (
                session.execute(select(Worker).where(Worker.state == "online")).scalars().all()
            )
            snapshots = _build_snapshots(list(workers))
            elig = eligible_workers(req, job.host_pin, snapshots)
            if not elig:
                return {"signaled": None, "reason": "no eligible workers online"}

            now = datetime.now(UTC)
            hosts = [ws.host for ws in elig]
            stmt = (
                select(Job)
                .where(
                    Job.worker.in_(hosts),
                    Job.state.in_([JobState.ASSIGNED.value, JobState.RUNNING.value]),
                    Job.preemptible.is_(True),
                    Job.signal.is_(None),
                )
                .order_by(Job.priority.asc(), Job.started_at.asc())
                .limit(1)
            )
            if not force:
                stmt = stmt.where(Job.priority < job.priority)
            candidate = session.execute(stmt).scalars().first()
            if candidate is None:
                reason = (
                    "no preemptible candidate on eligible workers"
                    if force
                    else "no preemptible candidate at lower priority on eligible workers (use force=true to override)"
                )
                return {"signaled": None, "reason": reason}

            candidate.signal = "preempt"
            candidate.warning = f"{_AUTO_PREEMPT_WARNING_PREFIX}{job.id} (manual)"
            candidate.warning_at = now
            session.commit()
            _emit_event(
                logs_dir,
                "auto_preempt",
                source="broker",
                job_id=candidate.id,
                project=candidate.project,
                cause="manual_force" if force else "manual",
                queued_job=job.id,
                worker=candidate.worker,
                queued_priority=job.priority,
                candidate_priority=candidate.priority,
            )
            return {"signaled": candidate.id, "worker": candidate.worker}

    @app.get("/jobs/{job_id}/signal")
    def get_signal(job_id: int):
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            return {"signal": job.signal}

    @app.post("/jobs/{job_id}/checkpoint-complete")
    def checkpoint_complete(job_id: int):
        """Worker observed the user's `jobd-checkpoint-complete` token in
        job stdout during a preempt grace window. Pure observability:
        appends a `checkpoint_complete` event to events.jsonl. No state
        change — the actual final state ('preempted'/'failed') still flows
        through /complete after the workload exits.
        """
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            _emit_event(
                logs_dir,
                "checkpoint_complete",
                source="broker",
                job_id=job_id,
                project=job.project,
                worker=job.worker,
            )
            return {"ok": True}

    @app.post("/jobs/{job_id}/refuse-admission", response_model=JobInfo)
    def refuse_admission(job_id: int, payload: AdmissionRefusal):
        """Worker observed live GPU contention (free_vram < required) at the
        moment of dispatch and refuses an already-assigned job. Broker
        reverts state to QUEUED, clears `worker` and `started_at`, and emits
        an `admission_blocked` event. The next /next-job poll re-routes via
        the normal matcher — typically to a different host (the heartbeat
        will catch up within ~5s) or back here once contention clears.

        409 if the job isn't in ASSIGNED state (race with /complete or a
        cancel signal).
        """
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            if job.state != JobState.ASSIGNED:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"job {job_id} is in state {job.state}; "
                        "can't refuse-admission (must be 'assigned')"
                    ),
                )
            prior_worker = job.worker

            if payload.reason == "cwd_missing":
                # The worker found the job's cwd absent on its host (e.g. a git
                # worktree that lives only on another machine). Record this host in
                # the job's exclusion set so the matcher won't re-offer it here,
                # then re-route — or fail cwd_unreachable if no eligible worker is
                # left that could have the path (instead of looping forever).
                excluded = _excluded_workers(job)
                if prior_worker and prior_worker not in excluded:
                    excluded.append(prior_worker)
                job.excluded_workers_json = json.dumps(excluded)
                req: JobRequires | None = None
                if job.requires_json and job.requires_json != "{}":
                    try:
                        req = JobRequires.model_validate_json(job.requires_json)
                    except Exception:
                        req = None
                # Eligibility for the re-route/terminal decision uses ONLINE
                # workers only: if the host that has the cwd is offline right now,
                # we fail fast (cwd_unreachable) rather than park the job QUEUED
                # waiting on a host that may never return — the user resubmits once
                # the host is back. (A momentarily-flapping host is the cost; the
                # alternative is the silent-queue this whole feature exists to kill.)
                online = (
                    session.execute(select(Worker).where(Worker.state == "online")).scalars().all()
                )
                snapshots = _build_snapshots(list(online))
                elig = [
                    w
                    for w in eligible_workers(req, job.host_pin, snapshots)
                    if w.host not in excluded
                ]

                # cwd_refused is emitted AFTER the commit in each branch below,
                # never before — a failed commit must not leave events.jsonl
                # claiming a refusal the DB never recorded (M3: this was the only
                # violator of the emit-after-commit invariant).
                def _emit_cwd_refused() -> None:
                    _emit_event(
                        logs_dir,
                        "cwd_refused",
                        source="broker",
                        job_id=job_id,
                        project=job.project,
                        worker=prior_worker,
                        cwd=payload.cwd,
                    )

                if not elig:
                    job.state = JobState.FAILED
                    job.worker = None
                    job.termination_reason = "cwd_unreachable"
                    # cwd_unreachable is a failed-side terminal: the job never
                    # ran, so its default-policy dependents can't proceed and
                    # must cascade-cancel (audit 2026-07-01 H2 — this path used
                    # to strand them in QUEUED forever).
                    cascaded = _cascade_on_parent_terminal(session, job, logs_dir)
                    session.commit()
                    try:
                        with (logs_dir / f"{job_id}.log").open("a") as lf:
                            lf.write(
                                f"[broker] cwd {payload.cwd!r} exists on no eligible "
                                f"worker (excluded: {excluded}); failing "
                                f"cwd_unreachable\n"
                            )
                    except OSError:
                        pass
                    _emit_cwd_refused()
                    _emit_cascade_cancellations(logs_dir, cascaded, job_id, JobState.FAILED.value)
                    if cascaded:
                        _wake_dispatchers()
                    session.refresh(job)
                    return _to_info(job)
                job.state = JobState.QUEUED
                job.worker = None
                job.started_at = None
                # Clear any pending cancel/preempt signal (M1): a requeue sends
                # the job to a FRESH worker, and a stale signal would make that
                # worker SIGTERM the re-run on its first /signal poll.
                job.signal = None
                session.commit()
                _emit_cwd_refused()
                # Job is back in QUEUED, excluding the refusing host — re-route it.
                _wake_dispatchers()
                session.refresh(job)
                return _to_info(job)

            # --- default: gpu_contention (unchanged) ---
            job.state = JobState.QUEUED
            job.worker = None
            job.started_at = None
            # Clear any pending signal (M1) — see the cwd_missing branch above.
            job.signal = None
            session.commit()
            _emit_event(
                logs_dir,
                "admission_blocked",
                source="broker",
                job_id=job_id,
                project=job.project,
                worker=prior_worker,
                required_gb=payload.required_gb,
                free_gb=payload.free_gb,
                foreign_pids=list(payload.foreign_pids),
                foreign_vram_gb=payload.foreign_vram_gb,
            )
            # Job is back in QUEUED — wake another worker to re-route it.
            _wake_dispatchers()
            session.refresh(job)
            return _to_info(job)

    @app.get("/jobs/{job_id}/output")
    def get_output(job_id: int, tail: int = 8192):
        """Return the last `tail` bytes of the job's captured stdout+stderr.

        The worker streams log chunks to /log, which the broker appends to
        logs_dir/<id>.log. This endpoint returns a tail of that file so
        callers can diagnose a failed job without SSHing to the worker.
        Returns 404 if the job doesn't exist, empty string if the file
        hasn't been created yet (worker crashed before any output).
        """
        with SessionLocal() as session:
            if session.get(Job, job_id) is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
        log_file = logs_dir / f"{job_id}.log"
        if not log_file.exists():
            return {"tail": "", "size_bytes": 0, "returned_bytes": 0, "truncated": False}
        size = log_file.stat().st_size
        tail = max(0, min(tail, 1_048_576))  # clamp to 1 MiB
        with log_file.open("rb") as f:
            if size > tail:
                f.seek(size - tail)
            data = f.read()
        return {
            "tail": data.decode("utf-8", errors="replace"),
            "size_bytes": size,
            "returned_bytes": len(data),
            "truncated": size > len(data),
        }

    @app.get("/wait/{job_id}")
    async def wait_job(job_id: int):
        log_file = logs_dir / f"{job_id}.log"

        async def event_generator():
            position = 0
            while True:
                # Read new log bytes since last position
                if log_file.exists():
                    with log_file.open("rb") as f:
                        f.seek(position)
                        chunk = f.read()
                    if chunk:
                        position += len(chunk)
                        yield {
                            "event": "log",
                            "data": chunk.decode("utf-8", errors="replace"),
                        }

                # Check job state
                with SessionLocal() as session:
                    job = session.get(Job, job_id)

                if job is None:
                    yield {"event": "error", "data": "no such job"}
                    return

                if JobState(job.state) in TERMINAL_STATES:
                    yield {
                        "event": "terminal",
                        "data": json.dumps({"state": job.state, "exit_code": job.exit_code}),
                    }
                    return

                await asyncio.sleep(0.5)

        return EventSourceResponse(event_generator())

    @app.post("/classify", response_model=ClassifyResult)
    def classify_endpoint(req: ClassifyRequest) -> ClassifyResult:
        from jobd.classifier import classify as _classify

        return _classify(req.cmd, state["classifier"])

    @app.post("/heartbeat")
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
            worker.state = "online"
            if hb.in_flight_pids is not None:
                worker.in_flight_pids_json = json.dumps(hb.in_flight_pids)
            orphan_records: list[tuple[int, str]] = []
            cascade_records: list[tuple[int, str, list[tuple[int, str]]]] = []
            requeued: list[int] = []
            if hb.in_flight_job_ids is not None:
                orphan_records, cascade_records, requeued = _reconcile_worker_in_flight(
                    session, hb.host, set(hb.in_flight_job_ids), logs_dir
                )
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
        if requeued:
            _wake_dispatchers()
        return {"ok": True}

    @app.get("/workers", response_model=list[WorkerInfo])
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
                )
                for w in workers
            ]

    @app.delete("/workers/{host}")
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

    @app.post("/next-job", response_model=JobInfo | None)
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
                all_queued = (
                    session.execute(select(Job).where(Job.state == JobState.QUEUED)).scalars().all()
                )
                queued: list[Job] = [j for j in all_queued if _deps_satisfied(j, session)]
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
                    skip_state: dict[int, str] = state["dispatch_skip_state"]
                    queued_by_id = {j.id: j for j in queued}
                    for job_id, reason in skips:
                        if skip_state.get(job_id) != reason:
                            skip_state[job_id] = reason
                            _emit_event(
                                logs_dir,
                                "dispatch_skip",
                                source="broker",
                                job_id=job_id,
                                project=queued_by_id[job_id].project,
                                worker=q.host,
                                reason=reason,
                            )
                    # Drop dedup entries for jobs no longer in the queue (so a
                    # job that ran or was cancelled doesn't leak memory, and a
                    # resubmit gets a fresh slot).
                    for stale in [k for k in skip_state if k not in queued_by_id]:
                        del skip_state[stale]
                    return None
                # Atomic claim: only one worker can transition queued -> assigned
                result = session.execute(
                    Job.__table__.update()  # type: ignore[attr-defined]  # SQLAlchemy: Table.update on __table__
                    .where(Job.id == pick.id, Job.state == JobState.QUEUED)
                    .values(state=JobState.ASSIGNED, worker=q.host, started_at=datetime.now(UTC))
                )
                session.commit()
                if result.rowcount == 0:  # type: ignore[attr-defined]  # SQLAlchemy: CursorResult.rowcount
                    return None  # lost race; re-attempt / re-wait
                session.refresh(pick)
                # SQLite stores naive UTC; mirror the sweeper pattern (line ~999)
                # so the subtract is naive-to-naive.
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
                return _to_info(pick)

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

    @app.post("/events", status_code=204)
    def ingest_event(body: EventIngest):
        """Generic audit-event ingester for non-broker sources.

        Source is allowlisted to {worker, hook, mcp} by the Pydantic Literal —
        `source="broker"` yields 422, preserving the single-writer property for
        broker-emitted events. Broker stamps `ts` server-side via _emit_event.
        """
        _emit_event(
            logs_dir,
            body.event,
            source=body.source,
            job_id=body.job_id,
            project=body.project,
            **body.payload,
        )
        return Response(status_code=204)

    @app.get("/events")
    def get_events(
        since: str | None = None,
        project: str | None = None,
        event: str | None = None,
        job_id: int | None = None,
        source: str | None = None,
        limit: int = 1000,
    ):
        """Return filtered event-stream rows from events.jsonl (newest-last).

        Filters: since (relative `Nh|Nd|Nw` or ISO-8601), project, event,
        job_id, source. limit defaults to 1000, clamped to [1, 10000].

        Rows missing the `source` field (legacy pre-schema-v2) are skipped
        silently with a per-request INFO log. Malformed JSON lines and rows
        with unparseable `ts` are skipped defensively.
        """
        limit = max(1, min(10000, int(limit)))
        cutoff = _parse_since(since) if since else None

        def _match(row: dict) -> bool:
            # Rows missing `source` are legacy (pre-schema-v2) — excluded, same
            # as before. The reverse-reader handles the ts/cutoff early-stop.
            if "source" not in row:
                return False
            if project is not None and row.get("project") != project:
                return False
            if event is not None and row.get("event") != event:
                return False
            if job_id is not None and row.get("job_id") != job_id:
                return False
            if source is not None and row.get("source") != source:  # noqa: SIM103 — parallel guard-clause filter
                return False
            return True

        return _events.read_events(logs_dir, match=_match, cutoff=cutoff, limit=limit)

    @app.get("/projects")
    def list_projects():
        return _projects_to_jsonable(state["projects"])

    @app.post("/projects/{name}")
    def set_project_priority(name: str, payload: dict):
        priority = max(0, min(100, int(payload["priority"])))
        existing = state["projects"].get(name)
        if existing is None:
            state["projects"][name] = ProjectEntry(priority=priority)
        else:
            existing.priority = priority
        _persist_projects(state)
        return _projects_to_jsonable(state["projects"])

    @app.post("/projects/{name}/nudge")
    def nudge_project_priority(name: str, payload: dict):
        delta = int(payload["delta"])
        existing = state["projects"].get(name)
        if existing is None:
            base_entry = state["projects"].get("_default")
            base = base_entry.priority if base_entry is not None else 40
            new_priority = max(0, min(100, base + delta))
            state["projects"][name] = ProjectEntry(priority=new_priority)
        else:
            existing.priority = max(0, min(100, existing.priority + delta))
        _persist_projects(state)
        return _projects_to_jsonable(state["projects"])

    @app.post("/reload")
    def reload_config():
        state["projects"] = load_projects(state["paths"]["projects"])
        state["profiles"] = load_profiles(state["paths"]["profiles"])
        state["classifier"] = load_classifier_rules(state["paths"]["classifier"])
        return {"reloaded": True}

    @app.post("/resolve", response_model=ResolvedConfig)
    def resolve_job(req: JobSubmit) -> ResolvedConfig:
        """Dry-run submit: return the effective resolved config without
        enqueuing a job. Sources are tagged so callers can see *why* each
        field landed where it did.
        """
        profile_spec = None
        if req.profile:
            profile_spec = resolve_profile(state["profiles"], req.profile)
            if profile_spec is None:
                raise HTTPException(status_code=404, detail=f"unknown profile: {req.profile}")

        proj_defaults = resolve_project_defaults(state["projects"], req.project)

        # priority: existing logic (project base + delta).
        priority_value = resolve_priority(state["projects"], req.project, req.priority_delta)
        priority_source: ResolutionSource = "cli" if req.priority_delta != 0 else "project_default"
        if req.project not in state["projects"]:
            priority_source = "global"

        # host_pin
        host_pin_source: ResolutionSource
        if req.host_pin != "any":
            host_pin_value = req.host_pin
            host_pin_source = "cli"
        elif proj_defaults.host_pin:
            host_pin_value = proj_defaults.host_pin
            host_pin_source = "project_default"
        elif profile_spec and profile_spec.host_hint and profile_spec.host_hint != "any":
            host_pin_value = profile_spec.host_hint
            host_pin_source = "profile"
        else:
            host_pin_value = "any"
            host_pin_source = "global"

        # max_wall_s / idle_timeout_s — no profile-level default; fall through
        # CLI -> project -> global(None).
        max_wall_source: ResolutionSource
        if req.max_wall_s is not None:
            max_wall_value = req.max_wall_s
            max_wall_source = "cli"
        elif proj_defaults.max_wall_s is not None:
            max_wall_value = proj_defaults.max_wall_s
            max_wall_source = "project_default"
        else:
            max_wall_value = None
            max_wall_source = "global"

        idle_source: ResolutionSource
        if req.idle_timeout_s is not None:
            idle_value = req.idle_timeout_s
            idle_source = "cli"
        elif proj_defaults.idle_timeout_s is not None:
            idle_value = proj_defaults.idle_timeout_s
            idle_source = "project_default"
        else:
            idle_value = None
            idle_source = "global"

        ckpt_source: ResolutionSource
        if req.checkpoint_grace_s is not None:
            ckpt_value = req.checkpoint_grace_s
            ckpt_source = "cli"
        elif proj_defaults.checkpoint_grace_s is not None:
            ckpt_value = proj_defaults.checkpoint_grace_s
            ckpt_source = "project_default"
        else:
            ckpt_value = None
            ckpt_source = "global"

        # preemptible
        pre_source: ResolutionSource
        if req.preemptible is not None:
            pre_value = req.preemptible
            pre_source = "cli"
        elif proj_defaults.preemptible is not None:
            pre_value = proj_defaults.preemptible
            pre_source = "project_default"
        elif profile_spec is not None and profile_spec.preemptible:
            pre_value = profile_spec.preemptible
            pre_source = "profile"
        else:
            pre_value = False
            pre_source = "global"

        # requires
        req_source: ResolutionSource
        if req.requires is not None:
            req_value = req.requires.model_dump()
            req_source = "cli"
        elif proj_defaults.requires is not None:
            req_value = proj_defaults.requires.model_dump()
            req_source = "project_default"
        elif profile_spec is not None and profile_spec.requires is not None:
            req_value = profile_spec.requires.model_dump()
            req_source = "profile"
        else:
            req_value = None
            req_source = "global"

        # escalate_to_arc — only project-default or global today.
        arc_source: ResolutionSource
        if proj_defaults.escalate_to_arc:
            arc_value = True
            arc_source = "project_default"
        else:
            arc_value = False
            arc_source = "global"

        warning: str | None = None
        if req.project not in state["projects"] and "_default" in state["projects"]:
            warning = (
                f"project {req.project!r} has no entry in projects.yaml; using global defaults"
            )

        return ResolvedConfig(
            project=req.project,
            effective_priority=FieldResolution(value=priority_value, source=priority_source),
            effective_host_pin=FieldResolution(value=host_pin_value, source=host_pin_source),
            effective_max_wall_s=FieldResolution(value=max_wall_value, source=max_wall_source),
            effective_idle_timeout_s=FieldResolution(value=idle_value, source=idle_source),
            effective_checkpoint_grace_s=FieldResolution(value=ckpt_value, source=ckpt_source),
            effective_preemptible=FieldResolution(value=pre_value, source=pre_source),
            effective_requires=FieldResolution(value=req_value, source=req_source),
            effective_escalate_to_arc=FieldResolution(value=arc_value, source=arc_source),
            submit_warning=warning,
        )

    def _prune_old_jobs(now: datetime | None = None) -> int:
        """Delete terminal jobs and their per-job .log files whose ``finished_at``
        is older than ``JOBD_JOB_RETENTION_DAYS``. Returns the count pruned; 0
        days (the default) disables pruning entirely — opt-in so an existing
        deployment never silently loses history.

        Keeps the jobs table and log dir bounded by the retention window on a
        long-running broker (events.jsonl is bounded separately by size
        rotation). Freed SQLite pages are reused under WAL, so the DB file stays
        bounded without a global-locking VACUUM. Removing old terminal rows is
        safe for any still-pending dependents: ``_deps_satisfied`` already treats
        a pruned parent as satisfied.
        """
        raw = os.environ.get("JOBD_JOB_RETENTION_DAYS", "").strip()
        try:
            retention_days = int(raw) if raw else JOB_RETENTION_DAYS_DEFAULT
        except ValueError:
            retention_days = JOB_RETENTION_DAYS_DEFAULT
        if retention_days <= 0:
            return 0
        if now is None:
            now = datetime.now(UTC).replace(tzinfo=None)
        cutoff = now - timedelta(days=retention_days)
        terminal = [s.value for s in TERMINAL_STATES]
        with SessionLocal() as session:
            pruned_ids = list(
                session.execute(
                    select(Job.id).where(
                        Job.state.in_(terminal),
                        Job.finished_at.is_not(None),
                        Job.finished_at < cutoff,
                    )
                ).scalars()
            )
            if not pruned_ids:
                return 0
            session.execute(Job.__table__.delete().where(Job.id.in_(pruned_ids)))  # type: ignore[attr-defined]  # SQLAlchemy: Table.delete on __table__
            session.commit()
        # Unlink each per-job .log AFTER the rows are gone: a leftover log for a
        # deleted row is harmless disk, whereas deleting the log of a row that
        # survived (had the delete failed) would 404 a live `job output`.
        for jid in pruned_ids:
            try:
                (logs_dir / f"{jid}.log").unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                log.warning("retention: could not unlink %s.log: %s", jid, e)
        _emit_event(
            logs_dir,
            "jobs_pruned",
            source="broker",
            count=len(pruned_ids),
            retention_days=retention_days,
        )
        return len(pruned_ids)

    def _sweep_once():
        """One pass: reclaim orphans, mark offline workers."""
        # SQLite stores datetimes as naive UTC; compare naive-to-naive.
        now = datetime.now(UTC).replace(tzinfo=None)
        going_offline_records: list[tuple[str, datetime | None]] = []
        scheduling_timeout_records: list[tuple[int, str, int]] = []
        # Declared up here (not at the running-reclaim loop) so the
        # scheduling_timeout cascade above can append to them too; all emitted
        # once after the single commit below.
        orphan_records: list[tuple[int, str, str | None, datetime | None, str]] = []
        cascade_records: list[tuple[int, str, list[tuple[int, str]]]] = []
        with SessionLocal() as session:
            # Audit 2026-05-18 (runtime-zombies S3): expire queued jobs whose
            # caller-supplied scheduling_timeout_s elapsed. Hatchet pattern,
            # motivated by jobs 577/578 on server. Done FIRST so subsequent
            # sweeper passes (unmatcheable warning, blocker probe) don't
            # waste work on already-terminal rows.
            timeout_candidates = (
                session.execute(
                    select(Job).where(
                        Job.state == JobState.QUEUED,
                        Job.scheduling_timeout_s.is_not(None),
                    )
                )
                .scalars()
                .all()
            )
            for j in timeout_candidates:
                deadline_age_s = (now - j.submitted_at).total_seconds()
                if deadline_age_s <= j.scheduling_timeout_s:
                    continue
                # Guarded so a job the matcher claims (queued->assigned) at the
                # same instant isn't clobbered back to a terminal
                # scheduling_timeout: the sweeper reads QUEUED then writes,
                # racing the atomic claim. If the claim won, skip — the job is
                # dispatching, not stuck. (The orphan/reclaim writes below are
                # instead gated by their worker-liveness check, which a live
                # /complete's fresh heartbeat can't slip past.)
                won = _cas_state(
                    session,
                    j.id,
                    (JobState.QUEUED,),
                    state=JobState.SCHEDULING_TIMEOUT.value,
                    finished_at=now,
                    termination_reason="scheduling_timeout",
                    signal=None,
                )
                if not won:
                    continue
                # Cascade over the timed-out parent: it never dispatched, so it
                # can never produce the output a default-policy dependent needs.
                # Without this the child strands in QUEUED forever — the matcher
                # won't dispatch it (deps unsatisfied) and nothing else cancels
                # it. any-exit dependents are unblocked separately by
                # _deps_satisfied. Reverses the 2026-05-18 S3 "out of scope"
                # call, which left non-any-exit children stranded (audit
                # 2026-07-01 H2; the deps test only passed by calling the hook
                # manually).
                session.refresh(j)  # sync ORM state so the cascade sees the terminal
                cascaded = _cascade_on_parent_terminal(session, j, logs_dir)
                if cascaded:
                    cascade_records.append((j.id, j.state, cascaded))
                scheduling_timeout_records.append((j.id, j.project, int(j.scheduling_timeout_s)))
            # Mark workers offline past threshold (split SELECT-then-set so we
            # can emit one worker_offline event per transitioning worker).
            offline_cutoff = now - timedelta(seconds=OFFLINE_AFTER_SECONDS)
            going_offline = (
                session.execute(
                    select(Worker).where(
                        Worker.last_heartbeat < offline_cutoff,
                        Worker.state != "offline",
                    )
                )
                .scalars()
                .all()
            )
            for w in going_offline:
                w.state = "offline"
                # Capture into local Python values BEFORE commit to avoid
                # SQLA detached-instance issues when emitting after commit.
                going_offline_records.append((w.host, w.last_heartbeat))

            # Audit 2026-05-18 (runtime-zombies S6): stale-worker transition.
            # Workers silent for >threshold but <OFFLINE_AFTER get marked
            # `stale` so the matcher (which only sees state=='online' rows)
            # stops dispatching to them. The longer offline transition
            # above still wins for fully-dead workers. Configurable via
            # JOBD_STALE_WORKER_THRESHOLD_S.
            try:
                stale_threshold_s = int(
                    os.environ.get(
                        "JOBD_STALE_WORKER_THRESHOLD_S",
                        STALE_WORKER_THRESHOLD_S_DEFAULT,
                    )
                )
            except ValueError:
                stale_threshold_s = STALE_WORKER_THRESHOLD_S_DEFAULT
            stale_cutoff = now - timedelta(seconds=stale_threshold_s)
            going_stale = (
                session.execute(
                    select(Worker).where(
                        Worker.last_heartbeat < stale_cutoff,
                        Worker.state == "online",
                    )
                )
                .scalars()
                .all()
            )
            going_stale_records: list[tuple[str, datetime | None]] = []
            for w in going_stale:
                w.state = "stale"
                going_stale_records.append((w.host, w.last_heartbeat))

            # Reclaim assigned jobs whose worker is stale
            assigned = (
                session.execute(select(Job).where(Job.state == JobState.ASSIGNED)).scalars().all()
            )
            for j in assigned:
                reclaim_seconds = DEAD_WORKER_SECONDS
                if j.requires_json and j.requires_json != "{}":
                    try:
                        req = JobRequires.model_validate_json(j.requires_json)
                        if req.idempotent:
                            reclaim_seconds = IDEMPOTENT_RECLAIM_SECONDS
                    except Exception:
                        pass
                cutoff = now - timedelta(seconds=reclaim_seconds)
                w = session.execute(
                    select(Worker).where(Worker.host == j.worker)
                ).scalar_one_or_none()
                if w is None or w.last_heartbeat < cutoff:
                    j.state = JobState.QUEUED
                    j.worker = None
                    j.started_at = None
                    # Clear a pending cancel/preempt signal (M1): the reclaim
                    # re-dispatches to a fresh worker, which would otherwise
                    # honor the stale signal and kill the re-run on first poll.
                    j.signal = None

            # Pre-launch CRIT-2: reclaim RUNNING jobs whose worker has died.
            # A worker crash mid-run otherwise strands the job in RUNNING
            # forever — no /complete ever lands, dependents never unblock, and
            # /wait never returns. Idempotent jobs are safe to re-run, so we
            # requeue them like the ASSIGNED path above; non-idempotent jobs
            # already had side effects, so we transition them to ORPHANED (a
            # failed-side terminal state that cascades to dependents) rather
            # than silently re-running a job that may not be re-runnable.
            # (orphan_records / cascade_records declared at the top of the sweep
            # so the scheduling_timeout cascade shares them.)
            running = (
                session.execute(select(Job).where(Job.state == JobState.RUNNING)).scalars().all()
            )
            for j in running:
                # Wall-clock backstop (independent of worker liveness). Worker-
                # side enforcement (job_worker.poll_signals SIGTERMs at
                # max_wall_s) lives only in the worker's per-job monitor; if the
                # worker crashed mid-run and restarted with no memory of the
                # job, that enforcement never lands and no /complete is ever
                # posted, so the job is stranded RUNNING forever even though the
                # worker is heartbeating again. A healthy worker would have
                # terminated + reported within seconds of max_wall_s, far inside
                # the grace, so this never races one. wall_clock_exceeded is a
                # terminal "ran too long" condition, so orphan regardless of
                # idempotency (re-running would just blow the same wall clock).
                # Regression: stranded job 1364 (desktop host-power crash 2026-06).
                if j.max_wall_s is not None and j.started_at is not None:
                    grace = WALL_CLOCK_BACKSTOP_GRACE_SECONDS + (j.checkpoint_grace_s or 0)
                    if (now - j.started_at).total_seconds() > j.max_wall_s + grace:
                        last_hb = None
                        if j.worker is not None:
                            wk = session.execute(
                                select(Worker).where(Worker.host == j.worker)
                            ).scalar_one_or_none()
                            last_hb = wk.last_heartbeat if wk is not None else None
                        j.state = JobState.ORPHANED.value
                        j.finished_at = now
                        j.termination_reason = "wall_clock_exceeded"
                        j.signal = None
                        cascaded = _cascade_on_parent_terminal(session, j, logs_dir)
                        cascade_records.append((j.id, j.state, cascaded))
                        orphan_records.append(
                            (j.id, j.project, j.worker, last_hb, "wall_clock_exceeded")
                        )
                        continue
                reclaim_seconds = DEAD_WORKER_SECONDS
                idempotent = False
                if j.requires_json and j.requires_json != "{}":
                    try:
                        req = JobRequires.model_validate_json(j.requires_json)
                        if req.idempotent:
                            idempotent = True
                            reclaim_seconds = IDEMPOTENT_RECLAIM_SECONDS
                    except Exception:
                        pass
                cutoff = now - timedelta(seconds=reclaim_seconds)
                w = session.execute(
                    select(Worker).where(Worker.host == j.worker)
                ).scalar_one_or_none()
                if w is not None and w.last_heartbeat >= cutoff:
                    continue  # worker still alive — leave the job running
                last_hb = w.last_heartbeat if w is not None else None
                if idempotent:
                    j.state = JobState.QUEUED
                    j.worker = None
                    j.started_at = None
                    j.signal = None  # M1: clear stale signal before re-dispatch
                else:
                    # Set the string value (not the enum) so the cascade's
                    # `parent.state in {...value}` membership test fires.
                    j.state = JobState.ORPHANED.value
                    j.finished_at = now
                    j.termination_reason = "worker_died"
                    j.signal = None
                    cascaded = _cascade_on_parent_terminal(session, j, logs_dir)
                    cascade_records.append((j.id, j.state, cascaded))
                    orphan_records.append((j.id, j.project, j.worker, last_hb, "worker_died"))

            session.commit()
            # Reclaims/requeues and orphan cascades above may have made jobs
            # dispatchable — wake any long-polling workers.
            _wake_dispatchers()
            for jid, proj, wkr, last_hb, reason in orphan_records:
                _emit_event(
                    logs_dir,
                    "job_orphaned",
                    source="broker",
                    job_id=jid,
                    project=proj,
                    worker=wkr,
                    last_heartbeat=last_hb.isoformat() if last_hb else None,
                    termination_reason=reason,
                )
            for parent_id, parent_state, cascaded in cascade_records:
                _emit_cascade_cancellations(logs_dir, cascaded, parent_id, parent_state)
            for host, last_hb in going_offline_records:
                _emit_event(
                    logs_dir,
                    "worker_offline",
                    source="broker",
                    host=host,
                    last_heartbeat=last_hb.isoformat() if last_hb else None,
                )
            # Audit 2026-05-18 (runtime-zombies S6): worker_stale event so
            # the operator can see the matcher-eligibility flip in the
            # broker event log alongside the existing worker_offline emit.
            for host, last_hb in going_stale_records:
                _emit_event(
                    logs_dir,
                    "worker_stale",
                    source="broker",
                    host=host,
                    last_heartbeat=last_hb.isoformat() if last_hb else None,
                )
            for job_id, project, threshold_s in scheduling_timeout_records:
                _emit_event(
                    logs_dir,
                    "scheduling_timeout",
                    source="broker",
                    job_id=job_id,
                    project=project,
                    threshold_s=threshold_s,
                )

            # Soft unmatcheable warning: queued >60s + no online worker advertises caps
            stale_queued = (
                session.execute(
                    select(Job).where(
                        Job.state == JobState.QUEUED,
                        Job.submitted_at < now - timedelta(seconds=UNMATCHEABLE_THRESHOLD_SECONDS),
                    )
                )
                .scalars()
                .all()
            )
            workers = (
                session.execute(select(Worker).where(Worker.state == "online")).scalars().all()
            )
            snapshots = _build_snapshots(list(workers))
            host_list = [w.host for w in workers]
            for j in stale_queued:
                req = None
                if j.requires_json and j.requires_json != "{}":
                    try:
                        req = JobRequires.model_validate_json(j.requires_json)
                    except Exception:
                        continue
                shim = SimpleNamespace(
                    id=j.id,
                    priority=j.priority,
                    submitted_at=j.submitted_at,
                    host_pin=j.host_pin,
                    vram_gb=j.vram_gb,
                    ram_gb=j.ram_gb,
                    cpus=j.cpus,
                    requires=req,
                )
                # req=None means job matches any worker by definition — an empty
                # fleet is a queueing delay, not a capability mismatch.
                matcheable = req is None or any(selectors_only_match(shim, ws) for ws in snapshots)

                blocker_warning: str | None = None
                if matcheable:
                    age_s = (now - j.submitted_at).total_seconds()
                    if age_s >= QUEUE_BLOCKED_THRESHOLD_SECONDS:
                        elig = eligible_workers(req, j.host_pin, snapshots)
                        if elig:
                            candidate = _find_preemptible_candidate(elig, j, session, now)
                            if candidate is not None:
                                candidate.signal = "preempt"
                                candidate.warning = f"{_AUTO_PREEMPT_WARNING_PREFIX}{j.id}"
                                candidate.warning_at = now
                                _emit_event(
                                    logs_dir,
                                    "auto_preempt",
                                    source="broker",
                                    job_id=candidate.id,
                                    project=candidate.project,
                                    cause="sweeper",
                                    queued_job=j.id,
                                    worker=candidate.worker,
                                    queued_priority=j.priority,
                                    candidate_priority=candidate.priority,
                                    queue_age_s=int(age_s),
                                )
                            else:
                                blocker = _find_nonpreemptible_blocker(elig, session)
                                if blocker is not None:
                                    blocker_warning = (
                                        f"{_BLOCKED_WARNING_PREFIX}{int(age_s // 60)}m: "
                                        f"blocked by non-preemptible job {blocker.id} on "
                                        f"{blocker.worker}; preempt with `job preempt {blocker.id}`"
                                    )

                if not matcheable:
                    new_w = (
                        f"{_UNMATCHEABLE_WARNING_PREFIX} none of {host_list} "
                        f"advertise required capabilities"
                    )
                    if j.warning != new_w:
                        j.warning = new_w
                        j.warning_at = now
                        _emit_event(
                            logs_dir,
                            "sweep_warning",
                            source="broker",
                            job_id=j.id,
                            project=j.project,
                            warning_text=j.warning,
                        )
                elif blocker_warning is not None:
                    if j.warning != blocker_warning:
                        j.warning = blocker_warning
                        j.warning_at = now
                        _emit_event(
                            logs_dir,
                            "sweep_warning",
                            source="broker",
                            job_id=j.id,
                            project=j.project,
                            warning_text=j.warning,
                        )
                elif j.warning and (
                    j.warning.startswith(_UNMATCHEABLE_WARNING_PREFIX)
                    or j.warning.startswith(_BLOCKED_WARNING_PREFIX)
                ):
                    # State improved — clear stale unmatcheable/blocked warnings.
                    # "will queue behind" warnings set at submit are preserved
                    # since they describe a different (predictive) signal.
                    j.warning = None
                    j.warning_at = None
            session.commit()

        # Retention prune (own session, opt-in via JOBD_JOB_RETENTION_DAYS).
        _prune_old_jobs(now)

    # Expose as test seams at module scope
    globals()["_sweep_once"] = _sweep_once
    globals()["_prune_old_jobs"] = _prune_old_jobs
    globals()["_engine_for_testing"] = lambda: engine

    return app
