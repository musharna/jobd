"""Pure matching logic: does a job fit on a worker, and which job runs next."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


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


class QueuedJob(Protocol):
    id: int
    priority: int
    submitted_at: datetime
    host_pin: str
    vram_gb: float
    ram_gb: float
    cpus: int


def fits_on_worker(job: QueuedJob, w: WorkerSnapshot) -> bool:
    """True iff the job resource needs fit the worker headroom, honoring host pin."""
    effective_vram = w.free_vram_gb - w.unregistered_vram_gb - SAFETY_MARGIN_VRAM_GB
    if job.vram_gb > effective_vram:
        return False
    if job.ram_gb > w.free_ram_gb - SAFETY_MARGIN_RAM_GB:
        return False
    if job.cpus > w.idle_cpus + OVERSUBSCRIBE_CPU_ALLOWANCE:
        return False
    if job.host_pin not in (w.host, "any", *w.host_aliases):
        return False
    return True


def pick_next_job(jobs: list[QueuedJob], w: WorkerSnapshot) -> QueuedJob | None:
    """Select the best job for worker w: highest priority, FIFO tiebreak, fits-first.

    Jobs are considered in priority DESC, submit_time ASC order. The first that
    fits is chosen. Jobs that do not fit are skipped, not rejected — a later
    worker poll may find them matchable.
    """
    ordered = sorted(jobs, key=lambda j: (-j.priority, j.submitted_at))
    for j in ordered:
        if fits_on_worker(j, w):
            return j
    return None
