"""jobd worker daemon — one per host.

Responsibilities:
- Heartbeat every 5s with current resource snapshot
- Long-poll /next-job
- Execute assigned job in a transient systemd user-scope, stream logs back
- Poll /jobs/{id}/signal for cancel/preempt requests
- Report terminal status via /jobs/{id}/complete
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

import httpx
import psutil

from jobd import cgroup_walk as _cgroup_walk
from jobd import subreaper as _subreaper

from .capabilities import detect as _detect_caps

# subreaper + cgroup_walk ship in the jobd package alongside this worker module,
# so they import directly (no sys.path juggling). _REAPER_OK stays as the flag
# the job-finalize path reads; a broken import now fails loudly at load — a
# broken install — rather than silently disabling orphan reaping.
_REAPER_OK = True

try:
    import pynvml

    pynvml.nvmlInit()
    _NVML_OK = True
except Exception:
    _NVML_OK = False


HEARTBEAT_INTERVAL_S = 5.0
SIGNAL_POLL_INTERVAL_S = 2.0
POLL_TIMEOUT_S = 30.0
# Grace between a watchdog SIGTERM and the forced SIGKILL. Matches the broker
# cancel path's default grace so escalation is bounded the same way whether it
# comes from the kill timer or the post-loop proc.wait(). Env-tunable so ops can
# shorten it for fast-failing fleets (and the live escalation test can too).
WATCHDOG_KILL_GRACE_S = float(os.environ.get("JOBD_WORKER_WATCHDOG_KILL_GRACE_S", "60"))

# Bounded retry for the terminal /complete POST. The endpoint is idempotent
# (returns current info if the job is already terminal), so re-POSTing after a
# transient network error is safe and avoids losing the job's final state.
_COMPLETE_RETRY_BACKOFF_S = (1, 2, 4, 8)  # sleeps BETWEEN 5 attempts (~15s worst case)
# Bound the post-SIGKILL wait so a D-state / frozen-mount process can't wedge
# the worker slot forever (bare proc.wait() can block indefinitely).
_POST_KILL_WAIT_S = 30

_CAPS = _detect_caps()

# `tracked_pids` is mutated by job threads (add at dispatch, discard at
# finalize) while the heartbeat thread snapshots it for NVML foreign-VRAM
# accounting. A plain set copy isn't atomic w.r.t. concurrent add/discard, so
# every read/write goes through this lock via the helpers below.
_tracked_pids_lock = threading.Lock()


def _tracked_pids_add(tracked_pids: set[int], pid: int) -> None:
    with _tracked_pids_lock:
        tracked_pids.add(pid)


def _tracked_pids_discard(tracked_pids: set[int], pid: int) -> None:
    with _tracked_pids_lock:
        tracked_pids.discard(pid)


def _tracked_pids_snapshot(tracked_pids: set[int]) -> set[int]:
    """Return a consistent copy of `tracked_pids` for the heartbeat read path.

    Hold time is minimal — copy under lock, compute (NVML/cgroup walks) outside.
    """
    with _tracked_pids_lock:
        return set(tracked_pids)


def _post_event(client, event, *, job_id=None, project=None, **payload):
    """Best-effort audit-event POST to broker /events.

    Builds the schema-v2 envelope (broker stamps `ts` server-side per spec
    §3 Q2). Network and HTTP errors are swallowed to stderr — the worker
    hot path must not fail because of an audit write.
    """
    body: dict = {"source": "worker", "event": event, "payload": payload}
    if job_id is not None:
        body["job_id"] = job_id
    if project is not None:
        body["project"] = project
    try:
        client.post("/events", json=body, timeout=2.0)
    except Exception as e:
        print(f"[worker] /events POST failed ({event}): {e}", file=sys.stderr)


# #42 multi-tenant per-host co-routing. Default 1 preserves single-slot
# behavior. When >1, the worker may dispatch multiple jobs concurrently;
# resource_snapshot() reports raw NVML/psutil readings minus the sum of
# in-flight ads so the broker matcher only sees true-available capacity.
# The live admission gate at /next-job (#41) remains the safety net for
# overstated ads or foreign holders.
def _max_concurrent_jobs() -> int:
    raw = os.environ.get("JOBD_WORKER_MAX_CONCURRENT_JOBS", "1").strip()
    try:
        n = int(raw)
    except ValueError:
        return 1
    return max(1, n)


# Per-job in-flight allocation. Keyed by job_id; each entry carries the ad
# the matcher used (vram_gb, ram_gb, cpus) so `resource_snapshot` can
# subtract pending claims from raw NVML/psutil readings. Mutated under
# `_in_flight_lock` to keep heartbeat reads consistent with poll-loop
# add/remove.
_in_flight: dict[int, dict[str, float]] = {}
_in_flight_lock = threading.Lock()


def _allocations_total() -> tuple[float, float, int]:
    """Return (vram_gb, ram_gb, cpus) summed across in-flight jobs."""
    with _in_flight_lock:
        if not _in_flight:
            return 0.0, 0.0, 0
        v = sum(float(a.get("vram_gb") or 0) for a in _in_flight.values())
        r = sum(float(a.get("ram_gb") or 0) for a in _in_flight.values())
        c = sum(int(a.get("cpus") or 0) for a in _in_flight.values())
    return v, r, c


def _register_in_flight(job: dict) -> None:
    with _in_flight_lock:
        _in_flight[int(job["id"])] = {
            "vram_gb": effective_vram_request_gb_from_job(job),
            "ram_gb": float(job.get("ram_gb") or 0),
            "cpus": int(job.get("cpus") or 0),
        }


def _unregister_in_flight(job_id: int) -> None:
    with _in_flight_lock:
        _in_flight.pop(job_id, None)


# SIGTERM drain (docs/plans/sigterm-drain.md). `_drain_event` flips once when
# shutdown begins and is never cleared in production. `_drain_hooks` maps
# job_id -> callable(reason, grace_cap_s) that routes the job through its
# preempt machinery; run_job registers its hook right after Popen and
# unregisters at finalize. The hook map and the flag together close the
# dispatch/registration gap race: drain sets the flag BEFORE invoking hooks,
# and run_job re-checks the flag right after registering (plus refuses to
# Popen at all once draining).
_drain_event = threading.Event()
_drain_hooks: dict[int, Callable[[str, float], None]] = {}
_drain_hooks_lock = threading.Lock()

DRAIN_GRACE_DEFAULT_S = 60.0
DRAIN_JOIN_MARGIN_S = 15.0


def _drain_grace_s() -> float:
    """Per-job grace budget during a drain (JOBD_WORKER_DRAIN_GRACE_S,
    default 60). Caps each job's checkpoint_grace_s so a 300s checkpoint
    window can't pin shutdown past the systemd stop budget — raising this
    requires raising TimeoutStopSec in job-worker.service in step."""
    raw = os.environ.get("JOBD_WORKER_DRAIN_GRACE_S", "").strip()
    if not raw:
        return DRAIN_GRACE_DEFAULT_S
    try:
        v = float(raw)
    except ValueError:
        return DRAIN_GRACE_DEFAULT_S
    return v if v > 0 else DRAIN_GRACE_DEFAULT_S


def _register_drain_hook(job_id: int, hook: Callable[[str, float], None]) -> None:
    with _drain_hooks_lock:
        _drain_hooks[job_id] = hook


def _unregister_drain_hook(job_id: int) -> None:
    with _drain_hooks_lock:
        _drain_hooks.pop(job_id, None)


def drain_in_flight(
    job_threads: list[threading.Thread],
    *,
    grace_s: float,
    join_margin_s: float = DRAIN_JOIN_MARGIN_S,
) -> dict:
    """Signal every in-flight job through its drain hook, then join the job
    threads against one shared deadline (grace + margin). Threads still alive
    at the deadline are abandoned — counted, logged by the caller, and left
    for the broker's reconcile backstop. Returns {"signaled", "aborted"}."""
    _drain_event.set()
    with _drain_hooks_lock:
        hooks = list(_drain_hooks.items())
    for jid, hook in hooks:
        try:
            hook("worker_shutdown", grace_s)
        except Exception as e:
            print(f"[worker] drain hook for job {jid} failed: {e}", file=sys.stderr)
    deadline = time.monotonic() + grace_s + join_margin_s
    for t in job_threads:
        t.join(max(0.0, deadline - time.monotonic()))
    aborted = sum(1 for t in job_threads if t.is_alive())
    return {"signaled": len(hooks), "aborted": aborted}


def _make_shutdown_handler(stop_event: threading.Event):
    """Flag-only SIGTERM/SIGINT handler. The handler runs in the main thread,
    which may be inside an httpx call on the shared client — re-entering the
    client here can deadlock on its pool lock, and sys.exit() kills daemon job
    threads without running their finally blocks (workload never signaled,
    /complete never posted). All real shutdown work happens after the poll
    loop observes stop_event."""

    def _handler(_signum, _frame):
        stop_event.set()

    return _handler


def _graceful_shutdown(client, job_threads: list[threading.Thread]) -> dict:
    """Drain in-flight jobs, then post the worker_shutdown audit event with
    the drain summary. Runs in the main thread after the poll loop exits."""
    summary = drain_in_flight(job_threads, grace_s=_drain_grace_s())
    _post_event(client, "worker_shutdown", host=hostname(), **summary)
    return summary


_STALE_SCOPE_RE = re.compile(r"^jobd-\d+\.scope$")


def _sweep_stale_scopes() -> list[str]:
    """Kill leftover jobd-<id>.scope units from a previous worker incarnation
    (SIGTERM-drain Phase 3, docs/plans/sigterm-drain.md).

    Scopes live outside the worker service's cgroup, so workloads survive an
    undrained worker death. The broker's heartbeat reconcile then requeues
    their (idempotent) jobs — and a re-dispatch would double-execute against
    the still-running old workload. Sweeping at startup, before the first
    poll, closes that window. Any jobd-*.scope alive before this worker has
    dispatched anything is by definition stale: one jobd worker per user
    session (current deployment model).
    """
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return []
    try:
        out = subprocess.run(
            [
                systemctl,
                "--user",
                "list-units",
                "--type=scope",
                "--all",
                "--plain",
                "--no-legend",
                "jobd-*.scope",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except Exception as e:
        print(f"[worker] stale-scope sweep: list-units failed: {e}", file=sys.stderr)
        return []
    swept: list[str] = []
    for line in out.splitlines():
        fields = line.split()
        unit = fields[0] if fields else ""
        if not _STALE_SCOPE_RE.match(unit):
            continue
        try:
            scope_path = _cgroup_walk.resolve_user_scope_path(unit)
            if scope_path is not None:
                killed = _cgroup_walk.kill_scope(scope_path)
            else:
                killed = []
                subprocess.run(
                    [systemctl, "--user", "kill", "--signal=KILL", unit],
                    check=False,
                    timeout=5,
                )
        except Exception as e:
            print(f"[worker] stale-scope sweep: {unit}: {e}", file=sys.stderr)
            continue
        swept.append(unit)
        print(
            f"[worker] stale-scope sweep: killed {unit} ({len(killed)} pid(s))",
            file=sys.stderr,
        )
    return swept


def _running_count() -> int:
    """Live count of in-flight jobs, for the heartbeat `running` slot field."""
    with _in_flight_lock:
        return len(_in_flight)


def _in_flight_ids() -> list[int]:
    """Sorted in-flight job ids for the heartbeat `in_flight_job_ids` report —
    the broker reconciles its ASSIGNED/RUNNING claims for this host against
    it, catching jobs a restarted worker no longer knows about (SIGTERM-drain
    Phase 2, docs/plans/sigterm-drain.md)."""
    with _in_flight_lock:
        return sorted(_in_flight.keys())


# Issue #7 (per-job PID inventory): job_id -> top-level Popen pid, registered
# by run_job right after Popen. The heartbeat report expands each entry with
# the job's live scope-cgroup pids — the same ownership boundary
# _effective_owned_pids uses — so the broker can tag probed GPU holders as
# jobd-owned (`/gpu-holders` known/job_id/worker fields).
_in_flight_pids: dict[int, int] = {}
_in_flight_pids_lock = threading.Lock()


def _register_in_flight_pid(job_id: int, pid: int) -> None:
    with _in_flight_pids_lock:
        _in_flight_pids[job_id] = pid


def _unregister_in_flight_pid(job_id: int) -> None:
    with _in_flight_pids_lock:
        _in_flight_pids.pop(job_id, None)


def _in_flight_pid_map() -> dict[str, list[int]]:
    """{job_id: [pids]} for the heartbeat `in_flight_pids` report — the
    top-level Popen pid plus the job's live scope-cgroup pids. Keys are
    strings (JSON object keys). Best-effort on the cgroup read, mirroring
    _effective_owned_pids."""
    with _in_flight_pids_lock:
        items = list(_in_flight_pids.items())
    out: dict[str, list[int]] = {}
    for jid, pid in items:
        pids = [pid]
        if _REAPER_OK:
            try:
                scope_path = _cgroup_walk.resolve_user_scope_path(f"jobd-{jid}.scope")
                if scope_path is not None:
                    pids.extend(p for p in _cgroup_walk.list_scope_pids(scope_path) if p != pid)
            except Exception as e:
                print(
                    f"[worker] in-flight pid map: cgroup read failed for job {jid}: {e}",
                    file=sys.stderr,
                )
        out[str(jid)] = pids
    return out


def _effective_owned_pids(tracked_pids: set[int]) -> set[int]:
    """The full set of GPU pids this worker owns, for NVML foreign-VRAM exclusion.

    `tracked_pids` holds the single pid this worker Popen'd. That pid is the
    job's top-level process, but a GPU job's CUDA contexts can live in FORKED
    CHILDREN under different pids — DDP/accelerate/dataloader workers, or a
    bash -c that spawns the trainer. NVML reports whichever pid actually holds
    the VRAM, so excluding only `tracked_pids` mis-counts the worker's OWN
    children as foreign/unregistered VRAM. (Verified on an RTX 5090: a parent
    that forks a child holding the CUDA context shows NVML reporting the child
    pid, which is NOT proc.pid but IS in the scope cgroup.procs.)

    The scope cgroup is the true ownership boundary — every pid the job spawned
    lives in it (same model the cancel/reap paths use via cgroup_walk). So the
    owned set is `tracked_pids` unioned with the live pids in every in-flight
    job's scope cgroup. Fast-path / unwrapped jobs have no scope (resolve
    returns None → contributes nothing) and their real child pid is already in
    `tracked_pids`, so both cases are covered.

    Best-effort: cgroup reads race with systemd teardown; on any error we fall
    back to the tracked_pids we already have (never fewer than the old behavior).
    """
    owned = _tracked_pids_snapshot(tracked_pids)
    if not _REAPER_OK:
        return owned
    with _in_flight_lock:
        job_ids = list(_in_flight.keys())
    for jid in job_ids:
        try:
            scope_path = _cgroup_walk.resolve_user_scope_path(f"jobd-{jid}.scope")
            if scope_path is not None:
                owned.update(_cgroup_walk.list_scope_pids(scope_path))
        except Exception as e:  # best-effort; never block a heartbeat
            print(
                f"[worker] cgroup-walk owned-pid resolve failed for job {jid}: {e}",
                file=sys.stderr,
            )
    return owned


def _is_solo_in_flight() -> bool:
    """True when at most one job (this one) is registered in flight.

    Gates the reparented-orphan /proc sweep in run_job: that sweep treats any
    process reparented to this worker but not in tracked_pids as a leak, which
    is only sound when no OTHER job is running concurrently (a concurrent
    fast-path job's reparented descendant would otherwise look like an orphan
    and be killed). See run_job's finalize block.
    """
    with _in_flight_lock:
        return len(_in_flight) <= 1


def _reserve_and_dispatch(
    job: dict,
    *,
    max_concurrent: int,
    job_threads: list,
    run_in_thread,
) -> None:
    """Reserve in-flight capacity for `job` synchronously, then run it.

    Registering the reservation HERE — in the calling (poll-loop) thread,
    before this function returns — is what bounds concurrency. The poll loop's
    capacity gate reads ``len(_in_flight)`` on its next iteration; because the
    reservation is already recorded, it cannot claim more jobs against a stale
    (too-low) count and oversubscribe past ``max_concurrent``. The pre-fix code
    registered inside the worker thread, so between ``t.start()`` and the thread
    scheduling, the gate could re-poll and overcommit.

    The job ALWAYS runs in a daemon thread, even at ``max_concurrent == 1``.
    Inline execution would park the main thread inside proc.stdout.read() for
    the whole job, so a SIGTERM drain could not start until the job ended
    naturally (docs/plans/sigterm-drain.md). Bounded shutdown comes from the
    drain deadline plus systemd's stop-timeout KILL, not thread daemonness.
    ``run_in_thread`` is the callable that executes the job and unregisters it
    when done.
    """
    _register_in_flight(job)
    t = threading.Thread(target=run_in_thread, args=(job,), daemon=True)
    t.start()
    job_threads.append(t)


def hostname() -> str:
    h = socket.gethostname()
    return os.environ.get("JOBD_WORKER_HOST", h)


def nvidia_processes() -> list[tuple[int, int]]:
    """Return [(pid, MiB)] for all CUDA processes."""
    if not _NVML_OK:
        return []
    out: list[tuple[int, int]] = []
    for i in range(pynvml.nvmlDeviceGetCount()):
        handle = pynvml.nvmlDeviceGetHandleByIndex(i)
        try:
            procs = pynvml.nvmlDeviceGetComputeRunningProcesses(handle)
            for p in procs:
                out.append((p.pid, p.usedGpuMemory // (1024 * 1024) if p.usedGpuMemory else 0))
        except pynvml.NVMLError:
            pass
    return out


def nvidia_free_vram_gb() -> float:
    if not _NVML_OK:
        return 0.0
    try:
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        info = pynvml.nvmlDeviceGetMemoryInfo(handle)
        return round(info.free / (1024**3), 2)
    except pynvml.NVMLError:
        return 0.0


def compute_unregistered_vram(
    nvidia_procs: Iterable[tuple[int, int]], tracked_pids: set[int]
) -> float:
    """Return unregistered VRAM in GB (rounded 2dp)."""
    mib = sum(used for pid, used in nvidia_procs if pid not in tracked_pids)
    return round(mib / 1024.0, 2)


def compute_owned_vram(nvidia_procs: Iterable[tuple[int, int]], owned_pids: set[int]) -> float:
    """VRAM (GB) actually held right now by this worker's own jobd job pids —
    the mirror of compute_unregistered_vram. Used to avoid double-counting an
    in-flight job's reservation: NVML free already reflects VRAM the job has
    allocated, so only the still-unallocated part of its ad should be reserved
    on top (M6)."""
    mib = sum(used for pid, used in nvidia_procs if pid in owned_pids)
    return round(mib / 1024.0, 2)


def gpu_admission_check(required_gb: float, tracked_pids: set[int]) -> dict:
    """Live GPU contention check at the moment of decision.

    Returns:
      {
        "blocked": bool,         # free < required → would OOM if we took it
        "required_gb": float,
        "free_gb": float,
        "foreign_pids": list[int],   # CUDA holders not owned by this worker
        "foreign_vram_gb": float,    # sum of foreign holdings (rounded 2dp)
      }

    `foreign_pids` / `foreign_vram_gb` are diagnostic — they identify which
    non-broker-owned processes drove the contention, for surfacing in the
    job's warning text and in the broker's `admission_blocked` event.
    The gate decision itself is `free < required`; foreign-holder presence
    is informational, not part of the AND.

    Returns blocked=False when NVML isn't available or required_gb<=0
    (non-GPU jobs don't need this gate).
    """
    if not _NVML_OK or required_gb <= 0:
        return {
            "blocked": False,
            "required_gb": float(required_gb),
            "free_gb": 0.0,
            "foreign_pids": [],
            "foreign_vram_gb": 0.0,
        }
    free_gb = nvidia_free_vram_gb()
    procs = nvidia_processes()
    foreign = [(pid, mib) for pid, mib in procs if pid not in tracked_pids and mib > 0]
    foreign_pids = sorted(pid for pid, _ in foreign)
    foreign_vram_gb = round(sum(mib for _, mib in foreign) / 1024.0, 2)
    return {
        "blocked": free_gb < required_gb,
        "required_gb": float(required_gb),
        "free_gb": free_gb,
        "foreign_pids": foreign_pids,
        "foreign_vram_gb": foreign_vram_gb,
    }


# Deliberate mirror of jobd.matcher.GPU_IMPLICIT_FLOOR_GB /
# effective_vram_request_gb. Kept separate on purpose: the matcher decides who
# *claims* the job, this gate decides whether to *start* it under live
# contention. If you change the resolution rules, change both.
_GPU_IMPLICIT_FLOOR_GB = 2.0

# Mirrors the `# NO_GPU` override pattern in the example PreToolUse hook
# (examples/claude-code-hooks/jobd-block-gpu.sh) — a literal token in the job
# command that opts out of the worker-side live admission gate. Use case:
# the user knows the matcher's vram_gb is overstated (e.g., a small fine-
# tune that the cuda-32gb tier-tag overshot), or knows the foreign holder
# is about to exit. Distinct from `# NO_GPU`, which tells the PreToolUse
# hook the command isn't a GPU launch at all.
_CONCURRENT_OK_RE = re.compile(r"#\s*CONCURRENT_OK\b")


def command_has_concurrent_ok(cmd: list[str] | None) -> bool:
    """True if the job command contains the literal `# CONCURRENT_OK`
    bypass marker. The token is matched on the joined command string so
    it survives both `bash -c "... # CONCURRENT_OK"` and direct argv
    forms (`["python", "train.py", "#", "CONCURRENT_OK"]`).
    """
    if not cmd:
        return False
    return _CONCURRENT_OK_RE.search(" ".join(cmd)) is not None


def effective_vram_request_gb_from_job(job: dict) -> float:
    """Worker-side mirror of `jobd.matcher.effective_vram_request_gb`.

    Resolution order:
      1. Explicit `vram_gb`
      2. Max N across `cuda-Ngb` tags in `requires.needs`
      3. `_GPU_IMPLICIT_FLOOR_GB` when `requires.gpu is True`
      4. 0 — non-GPU job, gate is a no-op
    """
    vram = float(job.get("vram_gb") or 0)
    if vram > 0:
        return vram
    requires = job.get("requires") or {}
    needs = requires.get("needs") or []
    tier_max = 0
    for tag in needs:
        if isinstance(tag, str) and tag.startswith("cuda-") and tag.endswith("gb"):
            try:
                n = int(tag[len("cuda-") : -len("gb")])
            except ValueError:
                continue
            if n > tier_max:
                tier_max = n
    if tier_max > 0:
        return float(tier_max)
    if requires.get("gpu") is True:
        return _GPU_IMPLICIT_FLOOR_GB
    return 0.0


def _configured_aliases() -> list[str]:
    base = ["any", "any-gpu"] if _NVML_OK else ["any"]
    extra = os.environ.get("JOBD_WORKER_ALIASES", "")
    for name in (n.strip() for n in extra.split(",")):
        if name and name not in base:
            base.append(name)
    return base


_ROOT_CANDIDATES = ["/home", "/tmp", "/mnt/c", "/mnt/d", "/opt", "/var", "/data", "/scratch"]


def _detect_mount_roots() -> list[str]:
    """Return the subset of candidate root dirs that actually exist on this host.

    Override via JOBD_WORKER_MOUNT_ROOTS=/foo,/bar to pin the list explicitly
    (useful for test workers or hosts with non-standard mounts).
    """
    override = os.environ.get("JOBD_WORKER_MOUNT_ROOTS", "").strip()
    if override:
        return [p.strip() for p in override.split(",") if p.strip()]
    found: list[str] = []
    for r in _ROOT_CANDIDATES:
        try:
            if os.path.isdir(r):
                found.append(r)
        except OSError:
            pass
    return found


def resource_snapshot(tracked_pids: set[int]) -> dict:
    # Snapshot the tracked-pid set under its lock so a concurrent job-thread
    # add/discard can't tear the copy the NVML foreign-VRAM accounting reads.
    tracked_pids = _tracked_pids_snapshot(tracked_pids)
    vm = psutil.virtual_memory()
    # psutil.getloadavg() is cross-platform (os.getloadavg() raises OSError on
    # Windows). On Windows psutil emulates the load average over a rolling
    # window — the first reading may be 0.0 until the window fills.
    load1 = psutil.getloadavg()[0]
    cpu_count = psutil.cpu_count() or 0
    idle_cpus = max(0, int(cpu_count - load1))
    raw_free_vram = nvidia_free_vram_gb()
    raw_free_ram = round(vm.available / (1024**3), 2)
    # #42 + M6: reserve only the *unallocated* portion of in-flight jobs' VRAM
    # ads. raw_free_vram (NVML) already reflects VRAM a running job has
    # allocated, so subtracting its full ad on top double-counts and idles
    # capacity the GPU actually has (M6: a steady multi-slot worker understated
    # free VRAM by Σ(ads)). Reserve max(0, ad - already_allocated): the startup
    # window (job spawned, CUDA not yet initialized → owned_vram≈0) still gets
    # the full reservation so a second big job can't overcommit; a steady job
    # reserves ~0 because NVML already covers it. The live admission gate at
    # /next-job (#41) remains the safety net for overstated ads.
    nvidia_procs = nvidia_processes()
    owned_pids = _effective_owned_pids(tracked_pids)
    alloc_vram, alloc_ram, alloc_cpus = _allocations_total()
    owned_vram = compute_owned_vram(nvidia_procs, owned_pids)
    unallocated_vram = max(0.0, alloc_vram - owned_vram)
    free_vram = max(0.0, round(raw_free_vram - unallocated_vram, 2))
    free_ram = max(0.0, round(raw_free_ram - alloc_ram, 2))
    idle_cpus = max(0, idle_cpus - alloc_cpus)
    return {
        "host": hostname(),
        "free_vram_gb": free_vram,
        "unregistered_vram_gb": compute_unregistered_vram(nvidia_procs, owned_pids),
        "free_ram_gb": free_ram,
        "idle_cpus": idle_cpus,
        "host_aliases": _configured_aliases(),
        "arch": _CAPS.arch,
        "os": _CAPS.os,
        "gpu": _CAPS.gpu,
        "tags": list(_CAPS.tags),
        "mount_roots": _detect_mount_roots(),
        "max_concurrent": _max_concurrent_jobs(),
        "running": _running_count(),
        "in_flight_job_ids": _in_flight_ids(),
        "in_flight_pids": _in_flight_pid_map(),
    }


# mock kept for test imports
def pick_resource_snapshot_mock():
    return {
        "host": "test",
        "free_vram_gb": 30.0,
        "unregistered_vram_gb": 0.0,
        "free_ram_gb": 28.0,
        "idle_cpus": 10,
        "host_aliases": ["any"],
        "arch": "unknown",
        "os": "unknown",
        "gpu": False,
        "tags": [],
        "mount_roots": [],
        "max_concurrent": 1,
        "running": 0,
    }


def heartbeat_loop(client: httpx.Client, tracked_pids: set[int], stop_event: threading.Event):
    while not stop_event.is_set():
        try:
            snap = resource_snapshot(tracked_pids)
            client.post("/heartbeat", json=snap, timeout=10.0)
        except Exception as e:
            print(f"[worker] heartbeat error: {e}", file=sys.stderr)
        stop_event.wait(HEARTBEAT_INTERVAL_S)


def _env_int(name: str) -> int | None:
    """Read a non-negative int from env, or None if unset/invalid."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        v = int(raw)
        return v if v > 0 else None
    except ValueError:
        return None


def _missing_launcher_path(cmd: list[str], cwd: str) -> str | None:
    """If `cmd[0]` looks like a path (starts with /, ./, ../) and the
    resolved file doesn't exist or isn't executable, return the offending
    path. Otherwise return None.

    Catches the exit-127 silent-fail mode where dispatch succeeds but the
    workload dies instantly with no stdout because the launcher script
    was never committed / shipped (BACKLOG "First-byte smoke" piece 3,
    motivated by jobs 261/262 on 2026-04-30). PATH-resolved executables
    (`bash`, `python`, `ctest`) are not checked here — Popen's
    FileNotFoundError catches those at line ~436.
    """
    if not cmd:
        return None
    head = cmd[0]
    if not (head.startswith("/") or head.startswith("./") or head.startswith("../")):
        return None
    resolved = head if os.path.isabs(head) else os.path.normpath(os.path.join(cwd, head))
    if not os.path.isfile(resolved):
        return resolved
    if not os.access(resolved, os.X_OK):
        return resolved
    return None


def _post_complete_with_retry(client, job_id, payload, *, sleep=time.sleep) -> None:
    """POST the terminal /complete payload with bounded exponential backoff.

    5 attempts, sleeping 1/2/4/8s between them (~15s worst-case). The endpoint
    is idempotent, so a re-POST after a transient failure is safe. After the
    final failure, keep the historical behavior: log and continue. `sleep` is
    injectable so tests don't actually wait.
    """
    attempts = len(_COMPLETE_RETRY_BACKOFF_S) + 1
    for i in range(attempts):
        try:
            client.post(f"/jobs/{job_id}/complete", json=payload, timeout=10.0)
            return
        except Exception as e:
            print(
                f"[worker] complete POST error for job {job_id} (attempt {i + 1}/{attempts}): {e}",
                file=sys.stderr,
            )
            if i < len(_COMPLETE_RETRY_BACKOFF_S):
                sleep(_COMPLETE_RETRY_BACKOFF_S[i])


def _wait_after_kill(proc, job_id, *, timeout=_POST_KILL_WAIT_S) -> int:
    """proc.wait() bounded after a SIGKILL. If the kill doesn't land (D-state,
    frozen mount) the bare wait blocks forever and wedges the slot; cap it and
    fall through with rc=-9 so the normal completion path still runs."""
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print(
            f"[worker] job {job_id}: process unresponsive {timeout}s after SIGKILL, "
            "abandoning wait (rc=-9)",
            file=sys.stderr,
        )
        return -9


def _cancel_kill_timers(timers: list[threading.Timer], lock: threading.Lock) -> None:
    """Cancel every pending SIGKILL-escalation Timer and drain the list, so a
    job that exited on its own doesn't leave a Timer thread alive (holding the
    proc closure) for up to grace_s."""
    with lock:
        pending = list(timers)
        timers.clear()
    for t in pending:
        t.cancel()


def run_job(client: httpx.Client, job: dict, tracked_pids: set[int]) -> None:
    job_id = job["id"]
    cmd = job["cmd"]
    cwd = job["cwd"]

    # SIGTERM drain: a job claimed just before the drain flag flipped must not
    # start a workload the drain can no longer see. Complete it as
    # preempted/worker_shutdown so the broker record doesn't strand in
    # ASSIGNED (docs/plans/sigterm-drain.md, dispatch-side gap race).
    if _drain_event.is_set():
        print(f"[worker] job {job_id}: refusing to start, worker is draining", file=sys.stderr)
        _post_complete_with_retry(
            client,
            job_id,
            {
                "exit_code": None,
                "final_state": "preempted",
                "termination_reason": "worker_shutdown",
            },
        )
        return

    env = os.environ.copy()

    # Apply caller-submitted env vars (`job["env"]`) so `--env FOO=bar` actually
    # reaches the workload. These layer over the worker's inherited environment;
    # the jobd-internal JOBD_CHECKPOINT_* vars set below are written afterward
    # and intentionally take precedence. Trusted-tailnet feature: a caller who
    # can reach /submit can already run arbitrary commands, so env injection
    # grants no additional privilege (see docs/security.md).
    submitted_env = job.get("env") or {}
    if submitted_env:
        env.update({str(k): str(v) for k, v in submitted_env.items()})

    # Pre-dispatch cwd-exists check. A host-local cwd (e.g. a git worktree that
    # lives only on another host) is absent here even though the broker's coarse
    # mount_roots prefix filter (/home) passed the job to us. Refuse admission so
    # the broker re-routes to a host that has the path — instead of cd-failing to
    # exit 127. The broker excludes this host for this job, so it won't be
    # re-offered here (no hot loop).
    if not os.path.isdir(cwd):
        print(
            f"[worker] job {job_id}: cwd missing here ({cwd}); refusing admission",
            file=sys.stderr,
        )
        try:
            client.post(
                f"/jobs/{job_id}/refuse-admission",
                json={"reason": "cwd_missing", "cwd": cwd},
                timeout=10.0,
            )
        except Exception as e:
            print(
                f"[worker] cwd-missing refuse post failed for job {job_id}: {e}",
                file=sys.stderr,
            )
        return

    # Pre-dispatch launcher-exists check. Without this, a missing absolute-
    # or relative-path script silently exits 127 inside the systemd-run
    # scope and the user only finds out at the next manual `job list`
    # check. Fail loud at dispatch with termination_reason=launcher_missing.
    missing = _missing_launcher_path(cmd, cwd)
    if missing is not None:
        print(
            f"[worker] job {job_id}: launcher missing or non-executable: {missing}",
            file=sys.stderr,
        )
        try:
            client.post(
                f"/jobs/{job_id}/log",
                content=(
                    f"[worker pre-dispatch] launcher missing or non-executable: {missing}\n"
                ).encode(),
                timeout=10.0,
            )
            client.post(
                f"/jobs/{job_id}/complete",
                json={
                    "exit_code": 127,
                    "final_state": "failed",
                    "termination_reason": "launcher_missing",
                },
                timeout=10.0,
            )
        except Exception as post_err:
            print(
                f"[worker] launcher_missing POST error for job {job_id}: {post_err}",
                file=sys.stderr,
            )
        return

    # Per-job timeouts — submit-time value wins; otherwise worker-wide default
    # via env var; otherwise unbounded (None). Idle watchdog kills jobs that
    # produce zero new log bytes for N seconds; wall timeout kills jobs that
    # exceed N seconds of total runtime regardless of liveness.
    idle_timeout_s = job.get("idle_timeout_s") or _env_int("JOBD_WORKER_IDLE_TIMEOUT_S")
    max_wall_s = job.get("max_wall_s") or _env_int("JOBD_WORKER_MAX_WALL_S")
    # Per-job preemption grace window. None = use the worker default below
    # (60s); an explicit value overrides. Capped server-side at 300s so a
    # stuck checkpoint can't pin a slot.
    checkpoint_grace_s = job.get("checkpoint_grace_s")
    # Surface to the workload so jobd.client.install_preemption_handler
    # can compute time_remaining() inside the user's checkpoint_fn.
    env["JOBD_CHECKPOINT_GRACE_S"] = str(
        checkpoint_grace_s if checkpoint_grace_s is not None else 60
    )

    # Per-job durable checkpoint directory. Default root puts checkpoints at
    # ~/.local/share/jobd/checkpoints/<job_id>/ — persistent (not /tmp), so
    # resume survives worker restart. Workloads write here; cleanup is
    # workload-side.
    override_root = os.environ.get("JOBD_WORKER_CHECKPOINT_ROOT", "").strip()
    if override_root:
        default_root = Path(os.path.expanduser(override_root))
    else:
        default_root = (
            Path(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"))
            / "jobd"
            / "checkpoints"
        )
    ckpt_dir = default_root / str(job_id)
    ckpt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    env["JOBD_CHECKPOINT_DIR"] = str(ckpt_dir)

    # Wrap heavy work in a transient systemd user-scope. An earlier shim
    # did this implicitly via `exec systemd-run --user --scope -- "$@"`,
    # but that meant Popen's pid pointed at the systemd-run client, not at the
    # bash inside the scope — SIGTERM-to-pid was a no-op (2026-04-26 cancel-
    # latency bug). Naming the unit lets us signal the scope directly via
    # `systemctl --user kill`, which targets every process in the cgroup.
    systemd_run = shutil.which("systemd-run")
    scope_unit: str | None = None
    if job.get("fast_path") or systemd_run is None:
        full_cmd = cmd
    else:
        scope_unit = f"jobd-{job_id}.scope"
        mem_max = os.environ.get("JOBD_WORKER_MEM_MAX", "14G")
        swap_max = os.environ.get("JOBD_WORKER_SWAP_MAX", "4G")
        full_cmd = [
            systemd_run,
            "--user",
            "--scope",
            "--quiet",
            "--same-dir",
            f"--unit={scope_unit}",
            "-p",
            f"MemoryMax={mem_max}",
            "-p",
            f"MemorySwapMax={swap_max}",
            "--",
        ] + cmd

    print(f"[worker] starting job {job_id}: {full_cmd}", file=sys.stderr)
    try:
        proc = subprocess.Popen(
            full_cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            # Unbuffered: we stream the pipe with explicit read(4096) chunks below.
            # bufsize=1 (line buffering) is invalid in binary mode (text=False) — it
            # warns and falls back to a BufferedReader whose read(4096) blocks until
            # 4096 bytes accumulate, delaying log streaming. 0 = prompt chunk reads.
            bufsize=0,
            text=False,
        )
    except (OSError, FileNotFoundError) as e:
        print(f"[worker] failed to start job {job_id}: {e}", file=sys.stderr)
        try:
            client.post(
                f"/jobs/{job_id}/log",
                content=f"[worker setup error] {e}\n".encode(),
                timeout=10.0,
            )
            client.post(
                f"/jobs/{job_id}/complete",
                json={"exit_code": 127, "final_state": "failed"},
                timeout=10.0,
            )
        except Exception as post_err:
            print(f"[worker] complete POST error: {post_err}", file=sys.stderr)
        return
    # `proc.pid` is the job's top-level process (for a scope-wrapped job,
    # systemd-run --scope exec-replaces into it, so this pid lives in the scope
    # cgroup — it is NOT a transient client). But a GPU job's CUDA contexts can
    # live in forked CHILDREN under other pids (DDP/dataloader workers, a
    # bash -c that spawns the trainer). NVML reports whichever pid holds the
    # VRAM, so tracking only proc.pid would mis-count the worker's OWN children
    # as foreign. GPU foreign-VRAM accounting therefore feeds
    # compute_unregistered_vram / gpu_admission_check `_effective_owned_pids()`,
    # which unions tracked_pids with the live pids in each in-flight job's scope
    # cgroup. Without it, a multi-process GPU job inflated unregistered_vram_gb
    # (spurious contention) and, at max_concurrent>1, made the worker refuse its
    # own second job. (RTX 5090 verified, 2026-06-01.)
    _tracked_pids_add(tracked_pids, proc.pid)
    _register_in_flight_pid(job_id, proc.pid)
    # Tell the broker the subprocess is live: assigned -> running. Best-effort:
    # if the POST fails the job still runs and /complete will land the terminal
    # state. The broker idempotently no-ops if the state isn't ASSIGNED.
    try:
        client.post(f"/jobs/{job_id}/started", timeout=10.0)
    except Exception as e:
        print(f"[worker] /started POST error for job {job_id}: {e}", file=sys.stderr)

    stop_signal = threading.Event()
    got_signal: dict[str, str | None] = {"signal": None}
    termination_reason: dict[str, str | None] = {"reason": None}
    checkpoint_state = {"complete": False}
    job_started = time.monotonic()
    last_log_lock = threading.Lock()
    last_log_monotonic = [time.monotonic()]
    # First-output watchdog: distinct from idle_timeout (which arms forever
    # and resets per chunk). This deadline is "did the workload ever produce
    # a byte?" — fires once at job-start+threshold if first_output_landed is
    # still False, then disarms permanently on the first stdout byte. Worker-
    # wide default via JOBD_WORKER_FIRST_OUTPUT_TIMEOUT_S; no per-job override
    # in this surface (BACKLOG smoke piece 2; user picked worker-only).
    first_output_timeout_s = _env_int("JOBD_WORKER_FIRST_OUTPUT_TIMEOUT_S")
    first_output_landed = [False]

    # SIGKILL-escalation timers scheduled by poll_signals (one per cancel/preempt
    # signal). Cancelled once the process exits so the timer thread doesn't
    # linger for up to grace_s holding the proc closure alive.
    kill_timers: list[threading.Timer] = []
    kill_timers_lock = threading.Lock()

    def _signal_workload(sig_name: str) -> None:
        """SIGTERM/SIGKILL the workload. When wrapped in a systemd scope we
        must target the scope unit (the cgroup), not proc.pid — that pid is
        the systemd-run client, which exits as soon as the scope is set up.
        Signaling a dead pid is a silent no-op (2026-04-26 cancel-latency).
        """
        if scope_unit is not None:
            try:
                subprocess.run(
                    ["systemctl", "--user", "kill", f"--signal={sig_name}", scope_unit],
                    check=False,
                    timeout=5,
                )
                return
            except Exception as e:
                print(
                    f"[worker] systemctl kill {scope_unit} {sig_name} error: {e}",
                    file=sys.stderr,
                )
        # fast_path or systemd-run unavailable: signal the direct child.
        try:
            sig = signal.SIGKILL if sig_name == "KILL" else signal.SIGTERM
            proc.send_signal(sig)
        except Exception as e:
            print(f"[worker] proc.send_signal {sig_name} error: {e}", file=sys.stderr)

    termination_initiated = threading.Event()

    def _initiate_termination(kind: str, reason: str | None, grace_s: float) -> None:
        """Route the workload into the cancel/preempt kill path: record the
        signal, SIGTERM, and schedule the SIGKILL escalation. Idempotent —
        first caller wins (a broker signal and a worker drain can race).

        SIGKILL escalation: if the workload ignores SIGTERM (badly-behaved
        handler, native code in a critical section), the stdout-read loop
        blocks forever and the post-loop proc.wait(timeout=grace) never gets
        a chance to fire. The Timer force-kills so grace_s actually bounds
        the wait."""
        if termination_initiated.is_set():
            return
        termination_initiated.set()
        got_signal["signal"] = kind
        if reason is not None:
            termination_reason["reason"] = reason
        _signal_workload("TERM")

        def _kill_after_grace():
            if proc.poll() is None:
                print(
                    f"[worker] job {job_id}: {kind} grace {grace_s}s expired, sending SIGKILL",
                    file=sys.stderr,
                )
                _signal_workload("KILL")

        kill_timer = threading.Timer(grace_s, _kill_after_grace)
        with kill_timers_lock:
            kill_timers.append(kill_timer)
        kill_timer.start()

    def poll_signals():
        while not stop_signal.is_set():
            now = time.monotonic()
            # Wall-clock cap: total runtime exceeded. Trips even when the job
            # is producing output — useful for runaway training runs.
            if max_wall_s is not None and (now - job_started) > max_wall_s:
                print(
                    f"[worker] job {job_id}: max_wall_s={max_wall_s}s exceeded, killing",
                    file=sys.stderr,
                )
                _post_event(
                    client,
                    "watchdog_fired",
                    job_id=job_id,
                    project=job.get("project"),
                    host=hostname(),
                    reason="wall_timeout",
                    threshold_s=max_wall_s,
                )
                # Route through _initiate_termination so the watchdog kill arms
                # the same SIGKILL-escalation timer as broker cancel/preempt. A
                # bare SIGTERM here left SIGTERM-ignoring jobs pinning their slot
                # forever: poll_signals returns, the stdout-read loop blocks on
                # the still-open pipe, and the post-loop proc.wait(timeout=grace)
                # escalation is never reached.
                _initiate_termination("cancel", "wall_timeout", WATCHDOG_KILL_GRACE_S)
                return
            # First-output watchdog: fires once if the workload hasn't emitted
            # a single byte within first_output_timeout_s of job start.
            # Disarms permanently after first byte; idle_timeout takes over
            # for steady-state silence. Catches the silent-hang-from-start
            # mode that idle_timeout (typically configured for steady-state
            # cadence) is too slow to catch.
            if (
                first_output_timeout_s is not None
                and not first_output_landed[0]
                and (now - job_started) > first_output_timeout_s
            ):
                print(
                    f"[worker] job {job_id}: no output within "
                    f"first_output_timeout_s={first_output_timeout_s}s of start, killing",
                    file=sys.stderr,
                )
                _post_event(
                    client,
                    "watchdog_fired",
                    job_id=job_id,
                    project=job.get("project"),
                    host=hostname(),
                    reason="first_output_timeout",
                    threshold_s=first_output_timeout_s,
                )
                # See wall_timeout branch: arm the SIGKILL escalation timer.
                _initiate_termination("cancel", "first_output_timeout", WATCHDOG_KILL_GRACE_S)
                return
            # Idle watchdog: no log bytes for N seconds. Trips on hung jobs
            # whose process is alive but making no progress (the 2026-04-25
            # project-c failure mode — heartbeats green, output silent).
            if idle_timeout_s is not None:
                with last_log_lock:
                    silent_for = now - last_log_monotonic[0]
                if silent_for > idle_timeout_s:
                    print(
                        f"[worker] job {job_id}: no output for {silent_for:.0f}s "
                        f"(idle_timeout_s={idle_timeout_s}), killing",
                        file=sys.stderr,
                    )
                    _post_event(
                        client,
                        "watchdog_fired",
                        job_id=job_id,
                        project=job.get("project"),
                        host=hostname(),
                        reason="idle_timeout",
                        threshold_s=idle_timeout_s,
                    )
                    # See wall_timeout branch: arm the SIGKILL escalation timer.
                    _initiate_termination("cancel", "idle_timeout", WATCHDOG_KILL_GRACE_S)
                    return
            try:
                r = client.get(f"/jobs/{job_id}/signal", timeout=5.0)
                if r.status_code == 200:
                    sig = r.json().get("signal")
                    if sig in ("cancel", "preempt"):
                        if sig == "preempt":
                            grace_s = checkpoint_grace_s if checkpoint_grace_s is not None else 60
                        else:
                            grace_s = 60
                        _initiate_termination(sig, None, grace_s)
                        return
            except Exception:
                pass
            stop_signal.wait(SIGNAL_POLL_INTERVAL_S)

    def _drain_hook(reason: str, grace_cap_s: float) -> None:
        """Drain entry point: preempt this job with the job's own grace capped
        by the drain budget, so one slow checkpoint can't pin shutdown past
        the systemd stop timeout."""
        job_grace = float(checkpoint_grace_s if checkpoint_grace_s is not None else 60)
        _initiate_termination("preempt", reason, min(job_grace, grace_cap_s))

    _register_drain_hook(job_id, _drain_hook)
    # Gap race (drain flag flipped between the top-of-run_job check and the
    # registration above): the drain's hook snapshot may have missed us, so
    # self-terminate now that the flag is visible.
    if _drain_event.is_set():
        _drain_hook("worker_shutdown", _drain_grace_s())

    sig_thread = threading.Thread(target=poll_signals, daemon=True)
    sig_thread.start()

    # Sentinel the user's preemption handler prints (after their checkpoint
    # function returns) to tell the worker "checkpoint is durable, you can
    # let me exit." Detection is byte-level so it survives chunk boundaries.
    checkpoint_token = b"jobd-checkpoint-complete"
    token_tail_len = len(checkpoint_token) - 1
    token_buf = b""

    # stdout is always a pipe here (Popen above passes stdout=subprocess.PIPE),
    # so this never trips at runtime; it narrows IO[bytes] | None for the reader.
    assert proc.stdout is not None
    try:
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            with last_log_lock:
                last_log_monotonic[0] = time.monotonic()
            # Disarm the first-output watchdog permanently. Done under the
            # last_log_lock so the watchdog thread can't observe a torn
            # state on hosts where bool writes aren't atomic.
            if not first_output_landed[0]:
                first_output_landed[0] = True
            # Only watch for the token once we've signaled preempt — any
            # other appearance is the workload echoing it for some other
            # reason and we don't want to fire the observability event.
            if got_signal["signal"] == "preempt" and not checkpoint_state["complete"]:
                scan = token_buf + chunk
                if checkpoint_token in scan:
                    checkpoint_state["complete"] = True
                token_buf = scan[-token_tail_len:]
            try:
                client.post(f"/jobs/{job_id}/log", content=chunk, timeout=10.0)
            except Exception as e:
                print(f"[worker] log POST error: {e}", file=sys.stderr)
    finally:
        proc.stdout.close()
        stop_signal.set()

    if got_signal["signal"] == "preempt":
        grace = checkpoint_grace_s if checkpoint_grace_s is not None else 60
    elif got_signal["signal"]:
        grace = 60
    else:
        grace = None
    try:
        rc = proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        _signal_workload("KILL")
        rc = _wait_after_kill(proc, job_id)

    # Process has exited (or been abandoned): cancel any pending SIGKILL-
    # escalation timers so their threads don't linger holding the proc closure.
    _cancel_kill_timers(kill_timers, kill_timers_lock)

    if got_signal["signal"] == "preempt" and checkpoint_state["complete"]:
        try:
            client.post(f"/jobs/{job_id}/checkpoint-complete", timeout=10.0)
        except Exception as e:
            print(f"[worker] /checkpoint-complete POST error: {e}", file=sys.stderr)

    _unregister_drain_hook(job_id)
    _unregister_in_flight_pid(job_id)
    _tracked_pids_discard(tracked_pids, proc.pid)

    # Audit 2026-05-18 (runtime-zombies S4): cgroup-walk reap. Anything
    # still alive in the per-job scope cgroup after the workload exits is
    # a zombie candidate (child detached, double-forked past subreaper, or
    # spawned children that outlived the parent). SIGKILL them so they
    # don't accumulate as PPID=1 orphans holding GPU/RAM. The desktop
    # inference_server orphan (21h51m, 16 GB VRAM held) is the canonical
    # case this defends against.
    if _REAPER_OK and scope_unit is not None:
        try:
            scope_path = _cgroup_walk.resolve_user_scope_path(scope_unit)
            if scope_path is not None:
                reaped = _cgroup_walk.kill_scope(scope_path)
                if reaped:
                    print(
                        f"[worker] job {job_id}: cgroup-walk reaped "
                        f"{len(reaped)} surviving PID(s) in {scope_unit}: {reaped}",
                        file=sys.stderr,
                    )
        except Exception as e:
            print(
                f"[worker] job {job_id}: cgroup-walk error for {scope_unit}: {e}",
                file=sys.stderr,
            )

    # Audit 2026-05-18 spec-review (S4 missing-bullet): /proc reparented-
    # orphan sweep. Anything whose ppid is this worker but isn't in the
    # currently-tracked-PID set was reparented to us by the subreaper bit
    # and never registered as a job descendant. The cgroup-walk above
    # caught process-tree leaks inside the scope; this catches the
    # complementary case where a descendant escaped the scope before
    # reparenting (still observed via PR_SET_CHILD_SUBREAPER).
    #
    # Concurrency guard (max_concurrent > 1): this /proc sweep is GLOBAL — it
    # has no way to tell job A's leaked orphan from job B's still-running
    # fast-path descendant (which also reparents to this worker). Only sweep
    # when this is the sole in-flight job; a concurrent job's orphan is reaped
    # at the next idle moment instead. cgroup-walk above is per-scope and
    # stays unconditional, so scope-wrapped jobs lose no cleanup.
    if _REAPER_OK and _is_solo_in_flight():
        try:
            killed = _subreaper.sweep_and_kill_reparented_orphans(
                _tracked_pids_snapshot(tracked_pids)
            )
            if killed:
                print(
                    f"[worker] job {job_id}: subreaper /proc-sweep reaped "
                    f"{len(killed)} reparented orphan PID(s): {killed}",
                    file=sys.stderr,
                )
        except Exception as e:
            print(
                f"[worker] job {job_id}: subreaper /proc-sweep error: {e}",
                file=sys.stderr,
            )

    final_state = "completed"
    if got_signal["signal"] == "cancel":
        # Watchdog-triggered cancels surface as "failed" (not "cancelled") so
        # depends_on cascades fail children — a wall/idle timeout means the
        # job did NOT do its job, and downstream consumers should treat it
        # like any other failure. User-initiated cancels still land in
        # "cancelled" to keep the manual-abort surface clean.
        if termination_reason["reason"] in (
            "wall_timeout",
            "idle_timeout",
            "first_output_timeout",
        ):
            final_state = "failed"
        else:
            final_state = "cancelled"
    elif got_signal["signal"] == "preempt":
        final_state = "preempted"
    elif rc != 0:
        final_state = "failed"

    complete_payload = {"exit_code": rc, "final_state": final_state}
    if termination_reason["reason"] is not None:
        complete_payload["termination_reason"] = termination_reason["reason"]
    _post_complete_with_retry(client, job_id, complete_payload)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobd-url", default=os.environ.get("JOBD_URL", "http://127.0.0.1:8765"))
    args = parser.parse_args()

    # Audit 2026-05-18 (runtime-zombies S4): set PR_SET_CHILD_SUBREAPER so
    # orphaned descendants whose direct parent dies get reparented to us
    # (and reaped by cgroup-walk at job-finalize). Doesn't propagate
    # across systemd-run --scope — cgroup-walk is the complementary path.
    if _REAPER_OK:
        _subreaper.set_child_subreaper()

    # Phase 3: kill workloads a previous (undrained) incarnation left behind
    # BEFORE the first poll — their jobs may have been requeued by the
    # broker's heartbeat reconcile and could otherwise re-dispatch here while
    # the old process still runs.
    swept_scopes = _sweep_stale_scopes()

    tracked_pids: set[int] = set()
    stop_event = threading.Event()

    _token = os.environ.get("JOBD_API_TOKEN", "").strip()
    _headers = {"Authorization": f"Bearer {_token}"} if _token else {}
    # Identify this worker on every request so the broker can refuse /log,
    # /started, and /complete from a stale worker whose job was reclaimed and
    # re-dispatched after a partition (M2). Must match the `host` this worker
    # sends in /next-job (also hostname()), which becomes job.worker.
    _headers["X-Jobd-Worker"] = hostname()
    client = httpx.Client(base_url=args.jobd_url, timeout=60.0, headers=_headers)
    if swept_scopes:
        _post_event(client, "stale_scope_sweep", host=hostname(), units=swept_scopes)
    hb = threading.Thread(
        target=heartbeat_loop, args=(client, tracked_pids, stop_event), daemon=True
    )
    hb.start()

    shutdown = _make_shutdown_handler(stop_event)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    max_concurrent = _max_concurrent_jobs()
    print(
        f"[worker] starting on {hostname()} -> {args.jobd_url} "
        f"(max_concurrent_jobs={max_concurrent})",
        file=sys.stderr,
    )

    def _dispatch_in_thread(job: dict) -> None:
        """Run one job to completion, then drop its in-flight reservation. The
        reservation is taken by `_dispatch` BEFORE this runs."""
        try:
            run_job(client, job, tracked_pids)
        except Exception as e:
            print(f"[worker] run_job error: {e}", file=sys.stderr)
        finally:
            _unregister_drain_hook(int(job["id"]))
            _unregister_in_flight_pid(int(job["id"]))
            _unregister_in_flight(int(job["id"]))

    job_threads: list[threading.Thread] = []

    def _dispatch(job: dict) -> None:
        """Reserve capacity for `job` synchronously, then run it. Delegates to
        the module-level `_reserve_and_dispatch` (which holds the reservation
        invariant + is unit-tested). The admission-refuse path never reaches
        here, so a refused job is never reserved."""
        _reserve_and_dispatch(
            job,
            max_concurrent=max_concurrent,
            job_threads=job_threads,
            run_in_thread=_dispatch_in_thread,
        )

    while not stop_event.is_set():
        try:
            # #42 capacity gate — when MAX_CONCURRENT_JOBS=1 this preserves
            # the historical single-slot behavior (poll only when idle).
            with _in_flight_lock:
                live = len(_in_flight)
            if live >= max_concurrent:
                time.sleep(1.0)
                continue
            snap = resource_snapshot(tracked_pids)
            q = {k: v for k, v in snap.items() if k != "host_aliases"}
            # Long-poll: ask the broker to hold this request until a job is
            # dispatchable or POLL_TIMEOUT_S elapses, instead of returning
            # instantly and forcing a 2s re-poll. Old brokers ignore the extra
            # field (Pydantic drops unknown keys) and return immediately — the
            # elapsed-time check on the no-job path below restores the backoff.
            q["wait_s"] = POLL_TIMEOUT_S
            t_poll = time.monotonic()
            r = client.post("/next-job", json=q, timeout=POLL_TIMEOUT_S + 5)
            if r.status_code == 200 and r.json() is not None:
                job = r.json()
                # Live VRAM gate at dispatch — broker's matcher used a
                # heartbeat snapshot up to ~5s old, but a foreign CUDA
                # process can land in that window and OOM us. Re-check
                # now and refuse the assignment if `free < required`.
                # `# CONCURRENT_OK` in the command opts out — the user
                # knows the matcher's vram_gb is overstated or that the
                # foreign holder is benign / about to exit.
                if command_has_concurrent_ok(job.get("cmd")):
                    print(
                        f"[worker] job {job['id']}: live admission gate "
                        "bypassed via # CONCURRENT_OK marker",
                        file=sys.stderr,
                    )
                    _dispatch(job)
                    continue
                required_gb = effective_vram_request_gb_from_job(job)
                admission = gpu_admission_check(required_gb, _effective_owned_pids(tracked_pids))
                if admission["blocked"]:
                    job_id = job["id"]
                    print(
                        f"[worker] refusing job {job_id}: "
                        f"required {admission['required_gb']:.1f} GB, "
                        f"free {admission['free_gb']:.1f} GB, "
                        f"foreign holders {admission['foreign_pids']} "
                        f"({admission['foreign_vram_gb']:.1f} GB)",
                        file=sys.stderr,
                    )
                    try:
                        client.post(
                            f"/jobs/{job_id}/refuse-admission",
                            json={
                                "required_gb": admission["required_gb"],
                                "free_gb": admission["free_gb"],
                                "foreign_pids": admission["foreign_pids"],
                                "foreign_vram_gb": admission["foreign_vram_gb"],
                            },
                            timeout=10.0,
                        )
                    except Exception as e:
                        print(
                            f"[worker] refuse-admission post failed for job {job_id}: {e}",
                            file=sys.stderr,
                        )
                    # Cool off so we don't immediately re-pull the same job
                    # while contention persists. Heartbeat updates the
                    # broker's `unregistered_vram_gb` view within ~5s.
                    time.sleep(5)
                    continue
                _dispatch(job)
            else:
                # No job. A long-polling broker already held us ~POLL_TIMEOUT_S,
                # so re-poll immediately; a fast null (wait_s=0 path, a non-200,
                # or an old broker ignoring wait_s) gets the 2s backoff so we
                # don't hot-loop the broker.
                if time.monotonic() - t_poll < POLL_TIMEOUT_S / 2:
                    time.sleep(2)
            job_threads = [t for t in job_threads if t.is_alive()]
        except Exception as e:
            print(f"[worker] poll error: {e}", file=sys.stderr)
            time.sleep(5)

    # stop_event set (SIGTERM/SIGINT): drain in-flight jobs in this thread —
    # the only place the shared client is safe to use during shutdown.
    print("[worker] stop requested — draining in-flight jobs", file=sys.stderr)
    summary = _graceful_shutdown(client, job_threads)
    print(
        f"[worker] drain complete: signaled={summary['signaled']} aborted={summary['aborted']}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
