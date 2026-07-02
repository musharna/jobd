"""Preemption / blocker probes and worker snapshot construction."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import select

from jobd.broker.constants import AUTO_PREEMPT_MIN_RUNTIME_SECONDS
from jobd.db import Job, Worker
from jobd.matcher import WorkerSnapshot, eligible_workers
from jobd.models import JobRequires, JobState


def _find_preemptible_candidate(
    elig: list[WorkerSnapshot],
    queued: Job,
    session,
    now: datetime,
) -> Job | None:
    """Find a preemptible job to displace in favor of a higher-priority queued
    job. Searches running/assigned jobs on eligible workers with priority
    strictly less than the queued job's priority, runtime above the
    AUTO_PREEMPT_MIN_RUNTIME_SECONDS floor, and no signal already set
    (don't double-fire).

    Returns the lowest-priority eligible candidate (most preemptable), or
    None if no candidate qualifies.
    """
    cutoff = now - timedelta(seconds=AUTO_PREEMPT_MIN_RUNTIME_SECONDS)
    hosts = [ws.host for ws in elig]
    if not hosts:
        return None
    return (
        session.execute(
            select(Job)
            .where(
                Job.worker.in_(hosts),
                Job.state.in_([JobState.ASSIGNED.value, JobState.RUNNING.value]),
                Job.preemptible.is_(True),
                Job.priority < queued.priority,
                Job.signal.is_(None),
                Job.started_at.is_not(None),
                Job.started_at <= cutoff,
            )
            .order_by(Job.priority.asc(), Job.started_at.asc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def _find_nonpreemptible_blocker(elig: list[WorkerSnapshot], session) -> Job | None:
    """If every eligible worker has a non-preemptible job in flight, return
    the first such blocker. Returns None if at least one eligible worker is
    free or running only preemptible jobs (the matcher can preempt those).
    """
    blocker: Job | None = None
    for ws in elig:
        running = (
            session.execute(
                select(Job).where(
                    Job.worker == ws.host,
                    Job.state.in_([JobState.ASSIGNED.value, JobState.RUNNING.value]),
                    Job.preemptible.is_(False),
                )
            )
            .scalars()
            .first()
        )
        if running is None:
            return None
        if blocker is None:
            blocker = running
    return blocker


def _serialization_warning(
    requires: JobRequires | None,
    host_pin: str,
    snapshots: list[WorkerSnapshot],
    session,
) -> str | None:
    """At submit time: if exactly one online worker can route this job AND
    that worker has a non-terminal job, return a 'will queue behind' string.
    Returns None otherwise.
    """
    elig = eligible_workers(requires, host_pin, snapshots)
    if len(elig) != 1:
        return None
    only = elig[0]
    busy = (
        session.execute(
            select(Job).where(
                Job.worker == only.host,
                Job.state.in_([JobState.ASSIGNED.value, JobState.RUNNING.value]),
            )
        )
        .scalars()
        .first()
    )
    if busy is None:
        return None
    return f"will queue behind job {busy.id} on {only.host}"


def _build_snapshots(workers: list[Worker]) -> list[WorkerSnapshot]:
    return [
        WorkerSnapshot(
            host=w.host,
            host_aliases=json.loads(w.host_aliases_json or "[]"),
            free_vram_gb=w.free_vram_gb,
            unregistered_vram_gb=w.unregistered_vram_gb,
            free_ram_gb=w.free_ram_gb,
            idle_cpus=w.idle_cpus,
            arch=w.arch,
            os=w.os,
            gpu=w.gpu,
            tags=json.loads(w.tags_json or "[]"),
            mount_roots=json.loads(w.mount_roots_json or "[]"),
        )
        for w in workers
    ]
