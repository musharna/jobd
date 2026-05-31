"""Cgroup-walk: enumerate and reap PIDs in a per-job systemd scope.

Complements jobd.subreaper. Subreaper alone is insufficient because
PR_SET_CHILD_SUBREAPER does NOT propagate across `systemd-run --scope`
(verified 2026-05-18 OSS survey). Any PID still living in the per-job
scope cgroup after job completion is a zombie candidate; cgroup_walk
SIGKILLs it.

Concrete failure this defends against: the desktop orphan
inference_server (PPID=1, 21h51m old, GPU context still held) — the
launcher died, the inference_server detached past the worker's
subreaper window, and stayed parked under systemd-pid-1 forever.
"""

from __future__ import annotations

import errno
import logging
import os
import signal
from pathlib import Path

log = logging.getLogger("jobd.cgroup_walk")


def list_scope_pids(scope_path: Path) -> list[int]:
    """Return the PIDs currently living in the given scope cgroup.

    scope_path is the cgroup directory (containing `cgroup.procs`).
    Returns [] if the path doesn't exist or has no procs file (job
    already cleaned up by systemd, or never had a scope wrap to begin
    with).

    Best-effort: malformed lines are skipped, the file may race with
    systemd's own teardown, etc.
    """
    if not isinstance(scope_path, Path):
        scope_path = Path(scope_path)
    procs_file = scope_path / "cgroup.procs"
    if not procs_file.exists():
        return []
    pids: list[int] = []
    try:
        raw = procs_file.read_text()
    except OSError as e:
        log.warning("cgroup_walk: read %s failed: %s", procs_file, e)
        return []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def kill_scope(scope_path: Path, sig: int = signal.SIGKILL) -> list[int]:
    """SIGKILL every PID still alive in the scope cgroup.

    Returns the list of PIDs we attempted to signal (whether the signal
    landed or ESRCH'd). Idempotent: re-running against an empty/already
    -reaped scope returns []. Defensive against races where the PID
    exited between read and kill (ESRCH is swallowed; EPERM is logged
    and counted).
    """
    pids = list_scope_pids(scope_path)
    if not pids:
        return []
    attempted: list[int] = []
    for pid in pids:
        try:
            os.kill(pid, sig)
        except OSError as e:
            if e.errno == errno.ESRCH:
                # Already dead — fine, count it as reaped.
                pass
            elif e.errno == errno.EPERM:
                log.warning(
                    "cgroup_walk: EPERM signaling pid=%d in %s (not owned by this uid?)",
                    pid,
                    scope_path,
                )
            else:
                log.warning("cgroup_walk: kill(%d, %d) failed: %s", pid, sig, e)
        attempted.append(pid)
    return attempted


def resolve_user_scope_path(scope_unit: str) -> Path | None:
    """Best-effort: return the cgroup path for a user-mode transient
    scope unit (e.g. 'jobd-42.scope'). Returns None if we can't locate
    a candidate path (no cgroup v2 fs, no user-NNN.slice, etc).

    cgroup v2 systemd-run --user scopes typically land at:
      /sys/fs/cgroup/user.slice/user-<UID>.slice/user@<UID>.service/
        app.slice/<scope_unit>
    """
    base = Path("/sys/fs/cgroup")
    if not base.exists():
        return None
    uid = os.getuid()
    candidates = [
        base
        / "user.slice"
        / f"user-{uid}.slice"
        / f"user@{uid}.service"
        / "app.slice"
        / scope_unit,
        base / "user.slice" / f"user-{uid}.slice" / f"user@{uid}.service" / scope_unit,
        base / scope_unit,
    ]
    for c in candidates:
        if c.exists():
            return c
    return None
