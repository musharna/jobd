"""Dual-signal GPU-holder probe: NVML compute-apps unioned with
fuser-of-/dev/nvidia*.

# METRIC_REFERENCE_OK - this is a diagnostic-surface module, not a
# bespoke probe formula. NVML enumeration mirrors jobd/worker/job_worker.py
# nvidia_processes() (the verified production probe); fuser parsing is
# a system-tool wrapper, not a derived metric.

NVML returns [N/A] memory for some held-GPU processes (driver/container
permissions, MIG carve-outs, certain CUDA contexts). The desktop
inference_server orphan (2026-05-18 probe) is the canonical case:
nvidia-smi listed the PID with [N/A] memory; only `fuser -v /dev/nvidia*`
surfaced it as a GPU holder.

The probe runs both signals and unions the results so the operator gets
one merged view. Diagnostic surface only — exposed via the broker's
GET /gpu-holders endpoint and the `job gpu-holders` CLI.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from glob import glob
from typing import Literal

log = logging.getLogger("jobd.gpu_holder_probe")

Source = Literal["nvml", "fuser", "both"]


@dataclass
class GpuHolder:
    pid: int
    gpu_id: int | None
    mem_mb: int | None
    source: Source
    # Audit 2026-05-18 spec-review (S5 fix): True when the broker recognizes
    # this PID as a currently-running job; False when unknown (the "kill or
    # STOP it?" surface). When the caller doesn't supply known_pids, every
    # row is `known=False` — operators can still see everything via the raw
    # probe and use the field as the filter shape going forward.
    known: bool = False


def _nvml_processes() -> list[tuple[int, int, int]]:
    """Return [(pid, gpu_id, mem_mb)] from NVML compute apps.

    Returns [] on any failure (no driver, no pynvml, NVML init error).
    Mirrors jobd/worker/job_worker.py:nvidia_processes but tagged per-GPU.
    """
    try:
        import pynvml  # type: ignore

        pynvml.nvmlInit()
    except Exception as e:
        log.debug("nvml unavailable: %s", e)
        return []
    out: list[tuple[int, int, int]] = []
    try:
        n = pynvml.nvmlDeviceGetCount()
    except Exception as e:
        log.warning("nvmlDeviceGetCount failed: %s", e)
        return []
    for i in range(n):
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            procs = pynvml.nvmlDeviceGetComputeRunningProcesses(handle)
        except Exception as e:
            log.warning("nvml device=%d enumerate failed: %s", i, e)
            continue
        for p in procs:
            mib = (p.usedGpuMemory // (1024 * 1024)) if p.usedGpuMemory else 0
            out.append((int(p.pid), i, int(mib)))
    return out


def _nvidia_dev_nodes() -> list[str]:
    """Return the list of /dev/nvidia* device nodes present on this host.

    Empty on WSL hosts that don't expose those nodes, on CPU-only hosts,
    and inside containers without device passthrough.
    """
    return sorted(glob("/dev/nvidia*"))


_FUSER_PID_RE = re.compile(r"\b(\d+)\b")


def _fuser_nvidia_pids() -> set[int]:
    """Return PIDs holding any /dev/nvidia* device, per `fuser -v`.

    Defensive: returns empty set if no devices exist, fuser is missing,
    fuser fails, or the output isn't parseable. The empty case isn't an
    error — it's the WSL/CPU-only path.
    """
    nodes = _nvidia_dev_nodes()
    if not nodes:
        return set()
    if shutil.which("fuser") is None:
        log.debug("fuser binary not on PATH; skipping device probe")
        return set()
    try:
        # fuser writes the table to stderr; PIDs land on stdout in some
        # versions and inline in stderr in others. Capture both.
        r = subprocess.run(
            ["fuser", "-v", *nodes],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("fuser invocation failed: %s", e)
        return set()
    # We don't gate on r.returncode — fuser returns 1 when no processes
    # are using the file, but we still want to parse stderr defensively.
    pids: set[int] = set()
    text = (r.stderr or "") + "\n" + (r.stdout or "")
    for line in text.splitlines():
        # Skip the header row that has "USER" + "PID" but no PID digit.
        if "PID" in line and "ACCESS" in line:
            continue
        for tok in _FUSER_PID_RE.findall(line):
            try:
                v = int(tok)
            except ValueError:
                continue
            # PID 1 is plausible. Cap at 2^22 to skip formatting/mask numbers.
            if 1 <= v < (1 << 22):
                pids.add(v)
    # Second-pass filter: require the PID to actually exist in /proc.
    # Drops formatting/header artifacts that survived the regex. On hosts
    # without /proc (non-Linux test environments) we fall back to the raw
    # set so synthetic-output tests still pass.
    real = set()
    for p in pids:
        try:
            with open(f"/proc/{p}/comm") as fh:
                fh.read()
            real.add(p)
        except OSError:
            continue
    return real if real else pids


def probe_gpu_holders(known_pids: set[int] | None = None) -> list[GpuHolder]:
    """Run both signals and return the unioned, pid-sorted list.

    Audit 2026-05-18 spec-review (S5 fix): when `known_pids` is supplied,
    each GpuHolder is tagged `known=(pid in known_pids)`. The probe still
    returns the FULL union — consumers can filter to `known=False` for
    the "unknown holder" surface (the kill-or-STOP decision case). When
    `known_pids` is None, every row is tagged `known=False`, which
    preserves the conservative "treat everything as unknown" default.
    """
    known: set[int] = set(known_pids) if known_pids is not None else set()
    nvml_rows = _nvml_processes()
    nvml_by_pid: dict[int, tuple[int, int]] = {}
    for pid, gpu_id, mem_mb in nvml_rows:
        # If a PID appears on multiple GPUs (multi-GPU process), keep the
        # first; mem_mb stays the per-GPU reading. Edge case in v1.
        nvml_by_pid.setdefault(pid, (gpu_id, mem_mb))
    fuser_pids = _fuser_nvidia_pids()

    out: list[GpuHolder] = []
    for pid in sorted(set(nvml_by_pid) | fuser_pids):
        in_nvml = pid in nvml_by_pid
        in_fuser = pid in fuser_pids
        is_known = pid in known
        if in_nvml and in_fuser:
            gpu_id, mem_mb = nvml_by_pid[pid]
            out.append(
                GpuHolder(pid=pid, gpu_id=gpu_id, mem_mb=mem_mb, source="both", known=is_known)
            )
        elif in_nvml:
            gpu_id, mem_mb = nvml_by_pid[pid]
            out.append(
                GpuHolder(pid=pid, gpu_id=gpu_id, mem_mb=mem_mb, source="nvml", known=is_known)
            )
        else:
            out.append(GpuHolder(pid=pid, gpu_id=None, mem_mb=None, source="fuser", known=is_known))
    return out
