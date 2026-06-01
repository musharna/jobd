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
from pathlib import Path
from typing import Iterable

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

_CAPS = _detect_caps()


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


def _running_count() -> int:
    """Live count of in-flight jobs, for the heartbeat `running` slot field."""
    with _in_flight_lock:
        return len(_in_flight)


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

    With ``max_concurrent > 1`` the job runs in a daemon thread (which
    unregisters it in a ``finally``); otherwise it runs inline. ``run_in_thread``
    is the callable that executes the job and unregisters it when done.
    """
    _register_in_flight(job)
    if max_concurrent > 1:
        t = threading.Thread(target=run_in_thread, args=(job,), daemon=True)
        t.start()
        job_threads.append(t)
    else:
        run_in_thread(job)


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
    vm = psutil.virtual_memory()
    # psutil.getloadavg() is cross-platform (os.getloadavg() raises OSError on
    # Windows). On Windows psutil emulates the load average over a rolling
    # window — the first reading may be 0.0 until the window fills.
    load1 = psutil.getloadavg()[0]
    cpu_count = psutil.cpu_count() or 0
    idle_cpus = max(0, int(cpu_count - load1))
    raw_free_vram = nvidia_free_vram_gb()
    raw_free_ram = round(vm.available / (1024**3), 2)
    # #42: subtract sum of in-flight ads so the broker matcher only sees
    # true-available capacity. Without this, a worker running one 24 GB
    # job still reports its full GPU as free (the foreign-PID accounting
    # below catches the live VRAM, but tracked_pids = jobd-owned PIDs are
    # subtracted from `unregistered`, not `free`). The live admission gate
    # at /next-job (#41) remains the safety net for overstated ads.
    alloc_vram, alloc_ram, alloc_cpus = _allocations_total()
    free_vram = max(0.0, round(raw_free_vram - alloc_vram, 2))
    free_ram = max(0.0, round(raw_free_ram - alloc_ram, 2))
    idle_cpus = max(0, idle_cpus - alloc_cpus)
    return {
        "host": hostname(),
        "free_vram_gb": free_vram,
        "unregistered_vram_gb": compute_unregistered_vram(nvidia_processes(), tracked_pids),
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


def run_job(client: httpx.Client, job: dict, tracked_pids: set[int]) -> None:
    job_id = job["id"]
    cmd = job["cmd"]
    cwd = job["cwd"]
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
            bufsize=1,
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
    # KNOWN ISSUE (deferred, 2026-05-31): when the job is systemd-scope-wrapped,
    # `proc.pid` is the systemd-run CLIENT pid, which exits as soon as the scope
    # is set up (see the scope_unit comments below) — the real workload (and its
    # CUDA contexts) run inside the jobd-<id>.scope cgroup under DIFFERENT,
    # untracked pids. So compute_unregistered_vram()/gpu_admission_check(), which
    # exclude tracked_pids, count this worker's OWN GPU job as "foreign" VRAM,
    # inflating the heartbeat's unregistered_vram_gb (spurious broker contention
    # signals) and, at max_concurrent>1, refusing the worker's own second job.
    # free_vram (from in-flight allocations) is unaffected, so routing stays
    # correct. Proper fix: track the scope cgroup's live pids (we already resolve
    # the scope path via cgroup_walk) and exclude those — but verifying that
    # cgroup-resident pids match NVML's reported GPU pids needs a real CUDA
    # process in a real systemd scope (real-execution doctrine), so it's deferred
    # to a dedicated GPU-host pass rather than fixed blind.
    tracked_pids.add(proc.pid)
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

    def poll_signals():
        while not stop_signal.is_set():
            now = time.monotonic()
            # Wall-clock cap: total runtime exceeded. Trips even when the job
            # is producing output — useful for runaway training runs.
            if max_wall_s is not None and (now - job_started) > max_wall_s:
                got_signal["signal"] = "cancel"
                termination_reason["reason"] = "wall_timeout"
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
                _signal_workload("TERM")
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
                got_signal["signal"] = "cancel"
                termination_reason["reason"] = "first_output_timeout"
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
                _signal_workload("TERM")
                return
            # Idle watchdog: no log bytes for N seconds. Trips on hung jobs
            # whose process is alive but making no progress (the 2026-04-25
            # project-c failure mode — heartbeats green, output silent).
            if idle_timeout_s is not None:
                with last_log_lock:
                    silent_for = now - last_log_monotonic[0]
                if silent_for > idle_timeout_s:
                    got_signal["signal"] = "cancel"
                    termination_reason["reason"] = "idle_timeout"
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
                    _signal_workload("TERM")
                    return
            try:
                r = client.get(f"/jobs/{job_id}/signal", timeout=5.0)
                if r.status_code == 200:
                    sig = r.json().get("signal")
                    if sig in ("cancel", "preempt"):
                        got_signal["signal"] = sig
                        _signal_workload("TERM")
                        # SIGKILL escalation: if the workload ignores SIGTERM
                        # (badly-behaved handler, native code in a critical
                        # section), the stdout-read loop blocks forever and
                        # the post-loop proc.wait(timeout=grace) never gets a
                        # chance to fire. Schedule a Timer to force-kill so
                        # checkpoint_grace_s actually bounds the wait.
                        if sig == "preempt":
                            grace_s = checkpoint_grace_s if checkpoint_grace_s is not None else 60
                        else:
                            grace_s = 60

                        def _kill_after_grace():
                            if proc.poll() is None:
                                print(
                                    f"[worker] job {job_id}: {sig} grace {grace_s}s "
                                    f"expired, sending SIGKILL",
                                    file=sys.stderr,
                                )
                                _signal_workload("KILL")

                        threading.Timer(grace_s, _kill_after_grace).start()
                        return
            except Exception:
                pass
            stop_signal.wait(SIGNAL_POLL_INTERVAL_S)

    sig_thread = threading.Thread(target=poll_signals, daemon=True)
    sig_thread.start()

    # Sentinel the user's preemption handler prints (after their checkpoint
    # function returns) to tell the worker "checkpoint is durable, you can
    # let me exit." Detection is byte-level so it survives chunk boundaries.
    checkpoint_token = b"jobd-checkpoint-complete"
    token_tail_len = len(checkpoint_token) - 1
    token_buf = b""

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
        rc = proc.wait()

    if got_signal["signal"] == "preempt" and checkpoint_state["complete"]:
        try:
            client.post(f"/jobs/{job_id}/checkpoint-complete", timeout=10.0)
        except Exception as e:
            print(f"[worker] /checkpoint-complete POST error: {e}", file=sys.stderr)

    tracked_pids.discard(proc.pid)

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
            killed = _subreaper.sweep_and_kill_reparented_orphans(set(tracked_pids))
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

    try:
        complete_payload = {"exit_code": rc, "final_state": final_state}
        if termination_reason["reason"] is not None:
            complete_payload["termination_reason"] = termination_reason["reason"]
        client.post(
            f"/jobs/{job_id}/complete",
            json=complete_payload,
            timeout=10.0,
        )
    except Exception as e:
        print(f"[worker] complete POST error: {e}", file=sys.stderr)


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

    tracked_pids: set[int] = set()
    stop_event = threading.Event()

    _token = os.environ.get("JOBD_API_TOKEN", "").strip()
    _headers = {"Authorization": f"Bearer {_token}"} if _token else {}
    client = httpx.Client(base_url=args.jobd_url, timeout=60.0, headers=_headers)
    hb = threading.Thread(
        target=heartbeat_loop, args=(client, tracked_pids, stop_event), daemon=True
    )
    hb.start()

    def shutdown(_signum, _frame):
        stop_event.set()
        _post_event(client, "worker_shutdown", host=hostname())
        sys.exit(0)

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
                admission = gpu_admission_check(required_gb, tracked_pids)
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
                time.sleep(2)
            job_threads = [t for t in job_threads if t.is_alive()]
        except Exception as e:
            print(f"[worker] poll error: {e}", file=sys.stderr)
            time.sleep(5)


if __name__ == "__main__":
    main()
