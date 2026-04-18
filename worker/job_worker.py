"""jobd worker daemon — single-file, one per host.

Responsibilities:
- Heartbeat every 5s with current resource snapshot
- Long-poll /next-job
- Execute assigned job under heavy-run, stream logs back
- Poll /jobs/{id}/signal for cancel/preempt requests
- Report terminal status via /jobs/{id}/complete
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Iterable

import httpx
import psutil

try:
    import pynvml
    pynvml.nvmlInit()
    _NVML_OK = True
except Exception:
    _NVML_OK = False


HEARTBEAT_INTERVAL_S = 5.0
SIGNAL_POLL_INTERVAL_S = 2.0
POLL_TIMEOUT_S = 30.0


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


def resource_snapshot(tracked_pids: set[int]) -> dict:
    vm = psutil.virtual_memory()
    load1 = os.getloadavg()[0]
    idle_cpus = max(0, int(psutil.cpu_count() - load1))
    return {
        "host": hostname(),
        "free_vram_gb": nvidia_free_vram_gb(),
        "unregistered_vram_gb": compute_unregistered_vram(nvidia_processes(), tracked_pids),
        "free_ram_gb": round(vm.available / (1024**3), 2),
        "idle_cpus": idle_cpus,
        "host_aliases": ["any", "any-gpu"] if _NVML_OK else ["any"],
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
    }


def heartbeat_loop(client: httpx.Client, tracked_pids: set[int], stop_event: threading.Event):
    while not stop_event.is_set():
        try:
            snap = resource_snapshot(tracked_pids)
            client.post("/heartbeat", json=snap, timeout=10.0)
        except Exception as e:
            print(f"[worker] heartbeat error: {e}", file=sys.stderr)
        stop_event.wait(HEARTBEAT_INTERVAL_S)


def run_job(client: httpx.Client, job: dict, tracked_pids: set[int]) -> None:
    job_id = job["id"]
    cmd = job["cmd"]
    cwd = job["cwd"]
    env = os.environ.copy()

    heavy_run = shutil.which("heavy-run")
    if heavy_run and not cmd[0].endswith("heavy-run"):
        full_cmd = [heavy_run, "--"] + cmd
    else:
        full_cmd = cmd

    print(f"[worker] starting job {job_id}: {full_cmd}", file=sys.stderr)
    proc = subprocess.Popen(
        full_cmd, cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=1, text=False,
    )
    tracked_pids.add(proc.pid)

    stop_signal = threading.Event()
    got_signal: dict[str, str | None] = {"signal": None}

    def poll_signals():
        while not stop_signal.is_set():
            try:
                r = client.get(f"/jobs/{job_id}/signal", timeout=5.0)
                if r.status_code == 200:
                    sig = r.json().get("signal")
                    if sig in ("cancel", "preempt"):
                        got_signal["signal"] = sig
                        try:
                            proc.send_signal(signal.SIGTERM)
                        except Exception:
                            pass
                        return
            except Exception:
                pass
            stop_signal.wait(SIGNAL_POLL_INTERVAL_S)

    sig_thread = threading.Thread(target=poll_signals, daemon=True)
    sig_thread.start()

    try:
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            try:
                client.post(f"/jobs/{job_id}/log", content=chunk, timeout=10.0)
            except Exception as e:
                print(f"[worker] log POST error: {e}", file=sys.stderr)
    finally:
        proc.stdout.close()
        stop_signal.set()

    grace = 60 if got_signal["signal"] else None
    try:
        rc = proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = proc.wait()

    tracked_pids.discard(proc.pid)

    final_state = "completed"
    if got_signal["signal"] == "cancel":
        final_state = "cancelled"
    elif got_signal["signal"] == "preempt":
        final_state = "preempted"
    elif rc != 0:
        final_state = "failed"

    try:
        client.post(
            f"/jobs/{job_id}/complete",
            json={"exit_code": rc, "final_state": final_state},
            timeout=10.0,
        )
    except Exception as e:
        print(f"[worker] complete POST error: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobd-url", default=os.environ.get("JOBD_URL", "http://100.113.204.41:8765"))
    args = parser.parse_args()

    tracked_pids: set[int] = set()
    stop_event = threading.Event()

    client = httpx.Client(base_url=args.jobd_url, timeout=60.0)
    hb = threading.Thread(
        target=heartbeat_loop, args=(client, tracked_pids, stop_event), daemon=True
    )
    hb.start()

    def shutdown(_signum, _frame):
        stop_event.set()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print(f"[worker] starting on {hostname()} -> {args.jobd_url}", file=sys.stderr)

    while not stop_event.is_set():
        try:
            snap = resource_snapshot(tracked_pids)
            q = {k: v for k, v in snap.items() if k != "host_aliases"}
            r = client.post("/next-job", json=q, timeout=POLL_TIMEOUT_S + 5)
            if r.status_code == 200 and r.json() is not None:
                run_job(client, r.json(), tracked_pids)
            else:
                time.sleep(2)
        except Exception as e:
            print(f"[worker] poll error: {e}", file=sys.stderr)
            time.sleep(5)


if __name__ == "__main__":
    main()
