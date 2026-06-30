"""Pure matching logic: does a job fit on a worker, and which job runs next."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, TypeVar

from jobd.models import JobRequires

SAFETY_MARGIN_VRAM_GB = 1.0
SAFETY_MARGIN_RAM_GB = 1.0
OVERSUBSCRIBE_CPU_ALLOWANCE = 2

# Implicit VRAM floor for jobs that say `requires.gpu == True` but don't
# specify vram_gb. Without this, the matcher used to skip the VRAM check
# entirely (job.vram_gb == 0) and dispatch to a GPU worker whose VRAM was
# fully held by a foreign (non-jobd) process — the 2026-04-25 SDXL/Pong
# collision. 2 GB is small enough not to gate normal CUDA workloads but
# big enough to drop a saturated GPU worker out of the matchee set.
GPU_IMPLICIT_FLOOR_GB = 2.0


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
    mount_roots: list[str] = field(default_factory=list)


class QueuedJob(Protocol):
    # Read-only members (declared as properties) so the protocol stays
    # covariance-friendly: a concrete row type whose fields are SQLAlchemy
    # `Mapped[...]` descriptors / a `requires` @property satisfies read-only
    # access, which a mutable (read-write) protocol attribute would reject
    # when used as a TypeVar bound or `Sequence[QueuedJob]` element.
    @property
    def id(self) -> int: ...
    @property
    def priority(self) -> int: ...
    @property
    def submitted_at(self) -> datetime: ...
    @property
    def host_pin(self) -> str: ...
    @property
    def vram_gb(self) -> float: ...
    @property
    def ram_gb(self) -> float: ...
    @property
    def cpus(self) -> int: ...
    @property
    def requires(self) -> JobRequires | None: ...


# Bound to the structural protocol so callers passing a concrete row type
# (e.g. the ORM `Job`) get that same type back from `pick_next_job` rather
# than the widened `QueuedJob`.
QueuedJobT = TypeVar("QueuedJobT", bound=QueuedJob)


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
    return all(t in w_tags for t in req.needs)


def effective_vram_request_gb(job: QueuedJob) -> float:
    """The VRAM (GB) the matcher should require for `job`.

    Resolution order:
      1. Explicit `job.vram_gb` (set via `--vram-required N` or
         `JobSubmit.vram_gb`)
      2. Tier-tag fallback: max N across `cuda-Ngb` tags in
         `requires.needs` (an 8 GB / 12 GB / 16 GB / 24 GB / 32 GB ladder
         maintained by `jobd/worker/capabilities.py`)
      3. `GPU_IMPLICIT_FLOOR_GB` (2 GB) when `requires.gpu is True`
      4. 0 — non-GPU jobs don't gate on VRAM
    """
    if job.vram_gb > 0:
        return float(job.vram_gb)
    requires = job.requires
    if requires is not None:
        tier_max = 0
        for tag in requires.needs:
            if tag.startswith("cuda-") and tag.endswith("gb"):
                try:
                    n = int(tag[len("cuda-") : -len("gb")])
                except ValueError:
                    continue
                if n > tier_max:
                    tier_max = n
        if tier_max > 0:
            return float(tier_max)
        if requires.gpu is True:
            return GPU_IMPLICIT_FLOOR_GB
    return 0.0


def fits_on_worker(job: QueuedJob, w: WorkerSnapshot) -> bool:
    """True iff selectors + host_pin + resource floor all allow the job."""
    if not _selectors_match(job.requires, w):
        return False
    if job.host_pin not in (w.host, "any", *w.host_aliases):
        return False
    effective_request = effective_vram_request_gb(job)
    if effective_request > 0:
        effective_vram = w.free_vram_gb - w.unregistered_vram_gb - SAFETY_MARGIN_VRAM_GB
        if effective_request > effective_vram:
            return False
    if job.ram_gb > w.free_ram_gb - SAFETY_MARGIN_RAM_GB:
        return False
    if job.cpus > w.idle_cpus + OVERSUBSCRIBE_CPU_ALLOWANCE:  # noqa: SIM103 — parallel guard-clause check
        return False
    return True


def explain_skip(jobs: Sequence[QueuedJob], w: WorkerSnapshot) -> list[tuple[int, str]]:
    """For each job that would NOT be picked on `w`, return (job_id, reason).

    Reasons are stable strings — public API:
      'arch_mismatch', 'os_mismatch', 'no_gpu', 'tags',
      'host_pin', 'vram', 'ram', 'cpus'.

    Mirrors the predicate chain in `fits_on_worker`; reports the FIRST
    failing predicate (short-circuit semantics). Cheap (no side effects,
    no DB). If a new check is added to `fits_on_worker`, it MUST be
    mirrored here too — the order matters.

    Note: jobs filtered out by the caller's mount-root check never reach
    this function — `mount_root` is therefore not in the reason set.
    """
    out: list[tuple[int, str]] = []
    for j in jobs:
        reason = _first_failing_predicate(j, w)
        if reason is not None:
            out.append((j.id, reason))
    return out


def _first_failing_predicate(j: QueuedJob, w: WorkerSnapshot) -> str | None:
    """Return the first reason `j` does not fit on `w`, or None if it fits.

    Order MUST match `fits_on_worker`. `_selectors_match` is decomposed
    here into 4 sub-checks (arch / os / gpu / tags) so each maps to a
    distinct reason string.
    """
    req = j.requires
    if req is not None:
        if req.arch != "any" and req.arch != w.arch:
            return "arch_mismatch"
        if req.os != "any" and req.os != w.os:
            return "os_mismatch"
        if req.gpu is not None and req.gpu != w.gpu:
            return "no_gpu"
        w_tags = set(w.tags)
        for t in req.needs:
            if t not in w_tags:
                return "tags"
    if j.host_pin not in (w.host, "any", *w.host_aliases):
        return "host_pin"
    effective_request = effective_vram_request_gb(j)
    if effective_request > 0:
        effective_vram = w.free_vram_gb - w.unregistered_vram_gb - SAFETY_MARGIN_VRAM_GB
        if effective_request > effective_vram:
            return "vram"
    if j.ram_gb > w.free_ram_gb - SAFETY_MARGIN_RAM_GB:
        return "ram"
    if j.cpus > w.idle_cpus + OVERSUBSCRIBE_CPU_ALLOWANCE:
        return "cpus"
    return None


def selectors_only_match(job: QueuedJob, w: WorkerSnapshot) -> bool:
    """Capability-only check (ignores resource floor + host_pin).

    Used by the 'soft unmatcheable' warning: if no worker's capabilities can
    ever match regardless of current load, surface a warning instead of silently
    queuing forever.
    """
    return _selectors_match(job.requires, w)


def eligible_workers(
    requires: JobRequires | None, host_pin: str, snapshots: list[WorkerSnapshot]
) -> list[WorkerSnapshot]:
    """Workers that match capability + host_pin (ignores current load).

    Used by scheduler-awareness warnings: a job whose `eligible_workers` is
    {} is unmatcheable; one with exactly one entry is single-slot serialized;
    one whose only entries all hold non-preemptible jobs is queue-blocked.
    """
    out: list[WorkerSnapshot] = []
    for w in snapshots:
        if not _selectors_match(requires, w):
            continue
        if host_pin not in (w.host, "any", *w.host_aliases):
            continue
        out.append(w)
    return out


def submit_preflight(
    requires: JobRequires | None,
    host_pin: str,
    all_known_workers: list[WorkerSnapshot],
) -> str | None:
    """Submit-time unsatisfiable-placement check.

    Snapshots here cover every worker the broker has *ever* registered
    (online + offline rows), so a typo'd `host_pin` doesn't get masked by
    a worker that just happens to be offline right now. Returns a
    user-facing warning string, or None when at least one known worker
    could satisfy the submit if it came back online.

    Catches:
      1. Unknown host_pin: `--host desktop-vm` (typo for `desktop`) —
         host_pin doesn't match any known hostname or alias.
      2. Unmet `requires.needs` tag — `--needs nonexistent-tag`.
      3. Unmet `requires.gpu=True` — fleet has no GPU workers.
      4. Unmet `requires.arch` / `requires.os`.

    Solves the 2026-04-28-filed silent-queue class: under either failure
    mode today, the job sits at queued indefinitely until the sweeper's
    60s soft-unmatcheable warning fires (or never, for host_pin typos
    which the sweeper doesn't check).
    """
    known_hosts: set[str] = set()
    for w in all_known_workers:
        known_hosts.add(w.host)
        known_hosts.update(w.host_aliases)
    if host_pin != "any" and host_pin not in known_hosts:
        if known_hosts:
            return (
                f"unsatisfiable host_pin {host_pin!r}: not in known workers {sorted(known_hosts)}"
            )
        return f"unsatisfiable host_pin {host_pin!r}: no known workers"
    candidates = [
        w for w in all_known_workers if host_pin == "any" or host_pin in (w.host, *w.host_aliases)
    ]
    if not candidates:
        return None
    if any(_selectors_match(requires, w) for w in candidates):
        return None
    bits: list[str] = []
    if requires is not None:
        for need in requires.needs:
            if not any(need in w.tags for w in candidates):
                bits.append(f"tag {need!r}")
        if requires.gpu is True and not any(w.gpu for w in candidates):
            bits.append("gpu=True")
        if requires.arch != "any" and not any(w.arch == requires.arch for w in candidates):
            bits.append(f"arch={requires.arch!r}")
        if requires.os != "any" and not any(w.os == requires.os for w in candidates):
            bits.append(f"os={requires.os!r}")
    scope = "any worker" if host_pin == "any" else f"host_pin={host_pin!r}"
    if bits:
        return f"unsatisfiable: no known worker for {scope} provides {', '.join(bits)}"
    return f"unsatisfiable: no known worker for {scope} matches requires"


def _covers(w: WorkerSnapshot, cwd: str) -> bool:
    """True iff the worker advertises a mount_root that prefixes cwd.

    Empty mount_roots = the worker doesn't advertise (old worker) = unknown;
    treated as 'can't assert', never as 'can't cover'.
    """
    return bool(w.mount_roots) and any(cwd.startswith(r) for r in w.mount_roots)


def cwd_routability(
    cwd: str, host_pin: str, all_known_workers: list[WorkerSnapshot]
) -> tuple[bool, str] | None:
    """Submit-time mount_roots reachability check for `cwd`.

    Returns None when routable or undecidable; (True, msg) for a hard deny
    (the caller should 400); (False, msg) for a soft warning (fold into the
    submit warnings). Generalizes the /mnt/c guard: it covers any host-local
    prefix a worker fails to advertise.

    Workers with empty mount_roots are 'unknown' and excluded from the deny
    decision, so stale/old workers never cause a false reject. A cwd under a
    prefix EVERY worker advertises (e.g. /home) is considered routable here —
    the worker-side os.path.isdir check (B) is the layer that catches a
    host-local path under a shared prefix (e.g. a git worktree).
    """
    # Workers that actually advertise something (can participate in the decision).
    advertising = [w for w in all_known_workers if w.mount_roots]
    if not advertising:
        return None  # nobody advertises roots -> can't assert anything

    if host_pin != "any":
        pinned = [w for w in advertising if host_pin in (w.host, *w.host_aliases)]
        if not pinned:
            return None  # pinned host doesn't advertise roots (or unknown) -> defer
        if any(_covers(w, cwd) for w in pinned):
            return None
        roots = sorted({r for w in pinned for r in w.mount_roots})
        return (
            True,
            f"cwd {cwd!r} is under no mount_root of host_pin={host_pin!r} "
            f"(roots: {roots}). Pass --host <a-host-that-has-it>, or stage the "
            f"data under a path that host advertises.",
        )

    # host_pin == "any": warn only if NO advertising worker covers it.
    if any(_covers(w, cwd) for w in advertising):
        return None
    return (
        False,
        f"cwd {cwd!r} is under no known worker's mount_roots; it may sit queued "
        f"or fail to route. Pass --host <the-host-that-has-it>, or stage under a "
        f"shared path (e.g. /tmp).",
    )


def gpu_contention_warning(
    requires: JobRequires | None,
    host_pin: str,
    snapshots: list[WorkerSnapshot],
) -> str | None:
    """Submit-time warning when a `gpu=True` job has no eligible host with
    real GPU headroom because foreign (non-jobd) processes hold most VRAM.

    Returns None when the job doesn't need GPU, when there are no eligible
    workers (the soft-unmatcheable warning covers that), or when at least
    one eligible worker has effective_vram >= GPU_IMPLICIT_FLOOR_GB. Fires
    only when EVERY eligible host is saturated — at that point the matcher
    won't dispatch and the user deserves a heads-up rather than a silent
    queue."""
    if requires is None or requires.gpu is not True:
        return None
    elig = eligible_workers(requires, host_pin, snapshots)
    if not elig:
        return None
    saturated: list[tuple[str, float]] = []
    for w in elig:
        effective_vram = w.free_vram_gb - w.unregistered_vram_gb - SAFETY_MARGIN_VRAM_GB
        if effective_vram < GPU_IMPLICIT_FLOOR_GB:
            saturated.append((w.host, w.unregistered_vram_gb))
    if len(saturated) < len(elig):
        return None
    parts = [f"{h} ({u:.0f} GB held by foreign processes)" for h, u in saturated]
    return f"GPU contention: all eligible workers saturated — {', '.join(parts)}"


def pick_next_job(jobs: Sequence[QueuedJobT], w: WorkerSnapshot) -> QueuedJobT | None:
    """Highest priority first, FIFO tiebreak. Skip jobs that don't fit."""
    ordered = sorted(jobs, key=lambda j: (-j.priority, j.submitted_at))
    for j in ordered:
        if fits_on_worker(j, w):
            return j
    return None
