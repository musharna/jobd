"""Adoption: register an already-running process as a job (docs/adoption.md).

The broker cannot see the process — it lives on a worker host — so this module
only records the claim and binds it to that worker. The worker verifies it
(pid + start time + owner) and watches it; see jobd.worker.adopt.

The row is created directly in RUNNING. That is deliberate, not a shortcut:
every path that returns a job to the queue (sweeper reclaim, reconcile, admission
refusal, retry) starts from QUEUED/ASSIGNED or requires `idempotent`, so a row
that is never in those states and never idempotent cannot be requeued — and a
requeued adopted job would be *launched*, running the user's command a second
time. `/next-job` also refuses any row carrying `adopt_pid`, for the same reason.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select

from jobd.broker.context import BrokerState
from jobd.broker.events import _emit_event
from jobd.broker.jobinfo import _to_info
from jobd.config import resolve_effective_config
from jobd.db import Job, Worker
from jobd.models import JobAdopt, JobInfo, JobRequires, JobState, JobSubmit
from jobd.procident import ADOPT_WORKER_TAG


def adopt_job(
    req: JobAdopt,
    *,
    session_factory: Callable[[], Any],
    state: BrokerState,
    logs_dir: Path,
) -> JobInfo:
    # Same identity/priority resolution a submit gets, so an adopted job is
    # listed and priced under the project a submit from that cwd would be.
    eff = resolve_effective_config(
        JobSubmit(cmd=req.cmd, cwd=req.cwd, project=req.project), state["projects"], None
    )
    project = eff.project
    project_label = eff.project_label if eff.project_label != project else None
    requires = JobRequires(gpu=True) if (req.gpu or req.vram_gb > 0) else None

    with session_factory() as session:
        worker = session.execute(select(Worker).where(Worker.host == req.host)).scalar_one_or_none()
        if worker is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"no worker registered as {req.host!r}; adoption needs the jobd worker "
                    "running on the host that owns the process (pass --host if its "
                    "JOBD_WORKER_HOST differs from the hostname)"
                ),
            )
        if worker.state != "online":
            raise HTTPException(
                status_code=409,
                detail=f"worker {req.host!r} is {worker.state}; nothing would watch the process",
            )
        if ADOPT_WORKER_TAG not in json.loads(worker.tags_json or "[]"):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"worker {req.host!r} (jobd {worker.version or 'unknown'}) does not advertise "
                    f"{ADOPT_WORKER_TAG!r}: it predates adoption or is not on Linux. "
                    "Upgrade that worker first."
                ),
            )
        duplicate = session.execute(
            select(Job.id).where(
                Job.state == JobState.RUNNING.value,
                Job.worker == req.host,
                Job.adopt_pid == req.pid,
                Job.adopt_start_ticks == req.start_ticks,
            )
        ).scalar()
        if duplicate is not None:
            raise HTTPException(
                status_code=409,
                detail=f"pid {req.pid} on {req.host!r} is already adopted as job {duplicate}",
            )
        now = datetime.now(UTC)
        job = Job(
            project=project,
            project_label=project_label,
            host_pin=req.host,
            priority=eff.priority.value,
            state=JobState.RUNNING,
            cmd_json=json.dumps(req.cmd),
            cwd=req.cwd,
            preemptible=False,
            vram_gb=req.vram_gb,
            worker=req.host,
            submitted_at=now,
            started_at=now,
            requires_json=requires.model_dump_json() if requires is not None else "{}",
            submitted_via=req.submitted_via,
            adopt_pid=req.pid,
            adopt_start_ticks=req.start_ticks,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        info = _to_info(job)
    _emit_event(
        logs_dir,
        "job_adopted",
        source="broker",
        job_id=info.id,
        project=info.project,
        worker=req.host,
        pid=req.pid,
        start_ticks=req.start_ticks,
        vram_gb=req.vram_gb,
    )
    return info


def adoption_fills_host(session, worker: Worker | None, host: str) -> bool:
    """True when `host` has a live adopted job and no slot left.

    Slots are otherwise enforced by the worker alone: it does not poll while full.
    But an adoption fills a slot from OUTSIDE the worker, and the worker only
    learns of it on its next heartbeat — while a long-poll it parked earlier,
    when it was idle, can still be answered with a job. The broker is the one
    party that knows about the adoption the instant it happens, so it holds the
    line at the claim. Hosts with no adopted job take the early return and see
    exactly the behaviour they always had.
    """
    in_flight = dict(
        session.execute(
            select(Job.adopt_pid.is_not(None), func.count())
            .where(
                Job.worker == host,
                Job.state.in_([JobState.ASSIGNED.value, JobState.RUNNING.value]),
            )
            .group_by(Job.adopt_pid.is_not(None))
        )
        .tuples()
        .all()
    )
    if not in_flight.get(True):
        return False
    slots = (worker.max_concurrent if worker is not None else None) or 1
    return sum(in_flight.values()) >= slots


def adopted_jobs_for_host(session, host: str) -> list[dict]:
    """The running adopted jobs bound to `host`, in the shape `/next-job` hands a
    worker. Returned on every `/heartbeat` response: it is how the worker learns
    of a new adoption, and how a restarted worker resumes watching old ones.

    RUNNING only. A job already orphaned as worker_died is not re-offered: the
    list would then carry every such row on every heartbeat forever. The process
    is untouched, so the remedy is to adopt it again."""
    rows = (
        session.execute(
            select(Job).where(
                Job.worker == host,
                Job.adopt_pid.is_not(None),
                Job.state == JobState.RUNNING.value,
            )
        )
        .scalars()
        .all()
    )
    return [_to_info(j).model_dump(mode="json") for j in rows]
