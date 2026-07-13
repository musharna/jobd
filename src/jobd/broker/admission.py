"""Admission refusal as a service: the broker's dispatch-time decision tree.

Extracted from the `/jobs/{job_id}/refuse-admission` route closure in `jobd.app`, which
held ~250 lines of the real admission engine inside a FastAPI handler.

A worker that sees live GPU contention at the moment of dispatch refuses the job it was
just assigned, and the broker decides what that means: requeue it, fail it terminally,
exclude this host for an unreachable cwd, or auto-preempt a blocker. Each branch carries
its own compare-and-swap, ORM refresh, dependency cascade and event teardown — and every
state write is CAS-guarded on `state=ASSIGNED`, so a refusal can never clobber a
`/complete` that lands in the same window (audit finding F1).

MONKEYPATCH TARGETS, since this module reads several patchable names:

`_cas_state` is patched by tests through the `jobd.app` namespace — but those tests
exercise `/cancel` and `/preempt-blockers`, which still live in jobd.app, so this move
does not touch them. Nothing currently patches _cas_state to reach admission. If you
ever need to, patch it HERE, in jobd.broker.admission: this is the module whose globals
these functions resolve from, and patching jobd.app instead would be a silent no-op.
See jobd/broker/submit.py's docstring for what that class of mistake has already cost.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from jobd.broker.events import _emit_event
from jobd.broker.jobinfo import _to_info
from jobd.broker.joblog import job_log_path
from jobd.broker.scheduling import _build_snapshots
from jobd.broker.state import (
    _cas_state,
    _cascade_on_parent_terminal,
    _emit_cascade_cancellations,
    _excluded_workers,
    _honor_pending_cancel,
    _reject_stale_worker,
    _requeue_or_honor_cancel,
)
from jobd.db import Job, Worker
from jobd.matcher import eligible_workers
from jobd.models import AdmissionRefusal, JobRequires, JobState


def refuse_admission(
    job_id: int,
    payload: AdmissionRefusal,
    x_jobd_worker: str | None,
    *,
    session_factory: Callable[[], Any],
    logs_dir: Path,
    wake_dispatchers: Callable[[], None],
) -> Any:
    """Worker observed live GPU contention (free_vram < required) at the
    moment of dispatch and refuses an already-assigned job. Broker
    reverts state to QUEUED, clears `worker` and `started_at`, and emits
    an `admission_blocked` event. The next /next-job poll re-routes via
    the normal matcher — typically to a different host (the heartbeat
    will catch up within ~5s) or back here once contention clears.

    409 if the job isn't in ASSIGNED state (race with /complete or a
    cancel signal), or if the reporting worker isn't the current owner
    (M2: a stale worker whose reclaimed job was re-dispatched must not be
    able to requeue/exclude the job now running on a different worker).
    Every state write below is a compare-and-swap on `state=ASSIGNED` so a
    refusal can't clobber a concurrent /complete that lands in the window.
    """
    with session_factory() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
        _reject_stale_worker(job.worker, x_jobd_worker, job_id, "admission refusal")
        if job.state != JobState.ASSIGNED:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"job {job_id} is in state {job.state}; "
                    "can't refuse-admission (must be 'assigned')"
                ),
            )
        prior_worker = job.worker

        # A pending user cancel makes the refusal disposition moot: the
        # user already asked to stop this job (and was told "cancelling"),
        # and the workload never started here. Honor it now — falling
        # through would either erase it on the requeue and re-run the job
        # elsewhere (audit 2026-07-05 A2), or mislabel a cancelled job as
        # failed/cwd_unreachable on the terminal branch. Signal-qualified
        # CAS; the per-branch teardowns below re-check for a cancel that
        # lands after this point.
        if _honor_pending_cancel(session, job_id, (JobState.ASSIGNED,), now=datetime.now(UTC)):
            session.refresh(job)  # sync ORM state so the cascade fires
            cascaded = _cascade_on_parent_terminal(session, job)
            session.commit()
            _emit_event(
                logs_dir,
                "job_cancelled",
                source="broker",
                job_id=job_id,
                project=job.project,
                prior_state=JobState.ASSIGNED.value,
                by="user",
                via="refuse_admission",
            )
            _emit_cascade_cancellations(logs_dir, cascaded, job_id, JobState.CANCELLED.value)
            if cascaded:
                wake_dispatchers()
            session.refresh(job)
            return _to_info(job)

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
            online = session.execute(select(Worker).where(Worker.state == "online")).scalars().all()
            snapshots = _build_snapshots(list(online))
            elig = [
                w for w in eligible_workers(req, job.host_pin, snapshots) if w.host not in excluded
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
                # Guarded terminal transition: if the job left ASSIGNED under
                # us (a concurrent /complete), don't clobber it. finished_at
                # is stamped like every other terminalizing write — without
                # it the row never ages out of retention pruning, which
                # filters on finished_at (audit 2026-07-05 F-3). A pending
                # signal is cleared for the same reason /complete clears it.
                if not _cas_state(
                    session,
                    job_id,
                    (JobState.ASSIGNED,),
                    state=JobState.FAILED,
                    worker=None,
                    finished_at=datetime.now(UTC),
                    signal=None,
                    termination_reason="cwd_unreachable",
                ):
                    session.refresh(job)
                    return _to_info(job)
                session.refresh(job)  # sync ORM state so the cascade sees FAILED
                # cwd_unreachable is a failed-side terminal: the job never
                # ran, so its default-policy dependents can't proceed and
                # must cascade-cancel (audit 2026-07-01 H2 — this path used
                # to strand them in QUEUED forever).
                cascaded = _cascade_on_parent_terminal(session, job)
                session.commit()
                try:
                    with job_log_path(logs_dir, job_id).open("a") as lf:
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
                    wake_dispatchers()
                session.refresh(job)
                return _to_info(job)
            # Guarded requeue (clears a stale preempt signal, M1). If the
            # job left ASSIGNED under us (concurrent /complete), don't
            # clobber it. A pending user CANCEL is honored, not erased: the
            # user was already told "cancelling", and the workload never
            # started here — requeuing would run it elsewhere anyway
            # (audit 2026-07-05 A2).
            outcome = _requeue_or_honor_cancel(
                session,
                job_id,
                (JobState.ASSIGNED,),
                now=datetime.now(UTC),
                state=JobState.QUEUED,
                worker=None,
                started_at=None,
                # refuse-admission re-route: the job never ran here, so keep
                # its scheduling_timeout clock running (audit 2026-07-12 M-1).
                reset_queue_clock=False,
            )
            if outcome == "lost":
                session.refresh(job)
                return _to_info(job)
            if outcome == "cancelled":
                session.refresh(job)  # sync ORM state so the cascade fires
                cascaded = _cascade_on_parent_terminal(session, job)
                session.commit()
                _emit_cwd_refused()
                _emit_event(
                    logs_dir,
                    "job_cancelled",
                    source="broker",
                    job_id=job_id,
                    project=job.project,
                    prior_state=JobState.ASSIGNED.value,
                    by="user",
                    via="refuse_admission",
                )
                _emit_cascade_cancellations(logs_dir, cascaded, job_id, JobState.CANCELLED.value)
                if cascaded:
                    wake_dispatchers()
                session.refresh(job)
                return _to_info(job)
            session.commit()
            _emit_cwd_refused()
            # Job is back in QUEUED, excluding the refusing host — re-route it.
            wake_dispatchers()
            session.refresh(job)
            return _to_info(job)

        # --- default: gpu_contention (unchanged) ---
        # Guarded requeue — same cancel-honoring teardown as the cwd branch.
        outcome = _requeue_or_honor_cancel(
            session,
            job_id,
            (JobState.ASSIGNED,),
            now=datetime.now(UTC),
            state=JobState.QUEUED,
            worker=None,
            started_at=None,
            # refuse-admission re-route: the job never ran here, so keep its
            # scheduling_timeout clock running (audit 2026-07-12 M-1).
            reset_queue_clock=False,
        )
        if outcome == "lost":
            session.refresh(job)
            return _to_info(job)
        if outcome == "cancelled":
            session.refresh(job)  # sync ORM state so the cascade fires
            cascaded = _cascade_on_parent_terminal(session, job)
            session.commit()
            _emit_event(
                logs_dir,
                "job_cancelled",
                source="broker",
                job_id=job_id,
                project=job.project,
                prior_state=JobState.ASSIGNED.value,
                by="user",
                via="refuse_admission",
            )
            _emit_cascade_cancellations(logs_dir, cascaded, job_id, JobState.CANCELLED.value)
            if cascaded:
                wake_dispatchers()
            session.refresh(job)
            return _to_info(job)
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
        wake_dispatchers()
        session.refresh(job)
        return _to_info(job)
