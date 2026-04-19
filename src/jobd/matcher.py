"""Pure matching logic: does a job fit on a worker, and which job runs next."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from jobd.models import JobRequires


SAFETY_MARGIN_VRAM_GB = 1.0
SAFETY_MARGIN_RAM_GB = 1.0
OVERSUBSCRIBE_CPU_ALLOWANCE = 2


@dataclass
class WorkerSnapshot:
    host: str
    host_aliases: list[str]
    free_vram_gb: float
    unregistered_vram_gb: float
    free_ram_gb: float
    idle_cpus: int
    arch: str = "unknown"
    os: str = "unknown"
    gpu: bool = False
    tags: list[str] = field(default_factory=list)


class QueuedJob(Protocol):
    id: int
    priority: int
    submitted_at: datetime
    host_pin: str
    vram_gb: float
    ram_gb: float
    cpus: int
    requires: JobRequires | None


def _selectors_match(req: JobRequires | None, w: WorkerSnapshot) -> bool:
    """True iff the worker satisfies every selector in `req`."""
    if req is None:
        return True
    if req.arch != "any" and req.arch != w.arch:
        return False
    if req.os != "any" and req.os != w.os:
        return False
    if req.gpu is not None and req.gpu != w.gpu:
        return False
    w_tags = set(w.tags)
    for t in req.needs:
        if t not in w_tags:
            return False
    return True


def fits_on_worker(job: QueuedJob, w: WorkerSnapshot) -> bool:
    """True iff selectors + host_pin + resource floor all allow the job."""
    if not _selectors_match(job.requires, w):
        return False
    if job.host_pin not in (w.host, "any", *w.host_aliases):
        return False
    effective_vram = w.free_vram_gb - w.unregistered_vram_gb - SAFETY_MARGIN_VRAM_GB
    if job.vram_gb > effective_vram:
        return False
    if job.ram_gb > w.free_ram_gb - SAFETY_MARGIN_RAM_GB:
        return False
    if job.cpus > w.idle_cpus + OVERSUBSCRIBE_CPU_ALLOWANCE:
        return False
    return True


def selectors_only_match(job: QueuedJob, w: WorkerSnapshot) -> bool:
    """Capability-only check (ignores resource floor + host_pin).

    Used by the 'soft unmatcheable' warning: if no worker's capabilities can
    ever match regardless of current load, surface a warning instead of silently
    queuing forever.
    """
    return _selectors_match(job.requires, w)


def pick_next_job(jobs: list[QueuedJob], w: WorkerSnapshot) -> QueuedJob | None:
    """Highest priority first, FIFO tiebreak. Skip jobs that don't fit."""
    ordered = sorted(jobs, key=lambda j: (-j.priority, j.submitted_at))
    for j in ordered:
        if fits_on_worker(j, w):
            return j
    return None
