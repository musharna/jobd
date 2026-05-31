"""PR_SET_CHILD_SUBREAPER glue for jobd workers.

Pattern from Ray's raylet, surveyed in the 2026-05-18 runtime-zombies
audit. Setting child-subreaper at worker startup means any orphaned
descendant whose direct parent dies gets reparented to the worker
instead of init/systemd-pid-1. The worker can then SIGKILL the rest of
its process tree on job completion.

Important caveat (survey gap-cell, verified during audit): this does
NOT propagate across `systemd-run --scope` boundary. The systemd-run
client process exits as soon as the scope is set up; any children
spawned inside the scope are owned by systemd-pid-1, not the worker.
Pair with cgroup_walk.kill_scope at job-finalize to catch those.
"""

from __future__ import annotations

import errno
import logging
import os
import signal
import sys
import time

log = logging.getLogger("jobd.subreaper")

PR_SET_CHILD_SUBREAPER = 36

_PLATFORM = sys.platform


def set_child_subreaper() -> bool:
    """Make the calling process a child-subreaper.

    Returns True on success, False on non-Linux platforms (no-op) or if
    the prctl call fails. Failures log a warning but do not raise — the
    worker should keep running even if the subreaper bit can't be set
    (degraded mode: orphaned descendants will reparent to init as before).
    """
    if _PLATFORM != "linux":
        log.warning(
            "set_child_subreaper: platform=%s is not linux; skipping prctl "
            "(orphaned descendants will reparent to init)",
            _PLATFORM,
        )
        return False
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        rc = libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
        if rc != 0:
            err = ctypes.get_errno()
            log.warning(
                "set_child_subreaper: prctl(PR_SET_CHILD_SUBREAPER, 1) "
                "returned rc=%d errno=%d; orphans will reparent to init",
                rc,
                err,
            )
            return False
    except OSError as e:
        log.warning("set_child_subreaper: libc/prctl error: %s", e)
        return False
    return True


def sweep_reparented_orphans(known_pids: set[int]) -> list[int]:
    """Find PIDs in /proc whose ppid == os.getpid(), set-difference against
    known_pids, return the orphan PIDs (caller decides kill policy).

    Audit 2026-05-18 spec-review (S4 missing-bullet): subreaper +
    cgroup_walk catch most zombies, but a descendant that reparents to
    THIS process (because we set PR_SET_CHILD_SUBREAPER) and isn't in
    the worker's in-flight job set is itself a leak indicator. The /proc
    walk surfaces them; the worker calls this at job-finalize.

    Uses os.listdir('/proc') + read('/proc/<pid>/stat') field 4 (ppid) to
    avoid pulling in psutil for this path (worker already has psutil but
    this module is also used by tests that can run without it).

    Returns [] on non-Linux (no /proc), on listdir error, or when no
    candidates remain after set-diff.
    """
    if _PLATFORM != "linux" or not os.path.isdir("/proc"):
        return []
    try:
        entries = os.listdir("/proc")
    except OSError as e:
        log.warning("sweep_reparented_orphans: /proc listdir failed: %s", e)
        return []
    my_pid = os.getpid()
    out: list[int] = []
    for name in entries:
        if not name.isdigit():
            continue
        try:
            pid = int(name)
        except ValueError:
            continue
        if pid == my_pid:
            continue
        ppid = _read_ppid(pid)
        if ppid is None:
            continue
        if ppid != my_pid:
            continue
        if pid in known_pids:
            continue
        out.append(pid)
    return sorted(out)


def _read_ppid(pid: int) -> int | None:
    """Parse field 4 (ppid) from /proc/<pid>/stat.

    Format: `<pid> (<comm>) <state> <ppid> ...`. The comm field can
    contain spaces and parentheses, so we slice from the LAST ')' to
    skip it entirely before splitting on whitespace. Returns None on
    any read/parse failure (the PID likely exited between listdir and
    open — racy but harmless).
    """
    try:
        with open(f"/proc/{pid}/stat") as fh:
            raw = fh.read()
    except OSError:
        return None
    try:
        # comm is the only field that may contain whitespace; it's
        # parenthesized. Slice from the last ')' to skip it.
        rparen = raw.rfind(")")
        if rparen < 0:
            return None
        rest = raw[rparen + 1 :].split()
        # rest[0] = state, rest[1] = ppid
        if len(rest) < 2:
            return None
        return int(rest[1])
    except (ValueError, IndexError):
        return None


def sweep_and_kill_reparented_orphans(
    known_pids: set[int],
    *,
    signal_first: int = signal.SIGTERM,
    escalate_after_s: float = 5.0,
) -> list[int]:
    """SIGTERM reparented orphans not in known_pids; SIGKILL survivors
    after escalate_after_s.

    Returns the list of PIDs signaled. ESRCH (already-exited) is
    swallowed silently; EPERM is logged but counted (we still got far
    enough to attempt the signal).
    """
    orphans = sweep_reparented_orphans(known_pids)
    if not orphans:
        return []
    signaled: list[int] = []
    for pid in orphans:
        try:
            os.kill(pid, signal_first)
            signaled.append(pid)
        except OSError as e:
            if e.errno == errno.ESRCH:
                # Raced with natural exit — fine.
                continue
            log.warning(
                "sweep_and_kill_reparented_orphans: kill(%d, %d) failed: %s",
                pid,
                signal_first,
                e,
            )
    if not signaled:
        return []
    # Brief wait, then SIGKILL anything still around.
    time.sleep(max(0.0, float(escalate_after_s)))
    for pid in list(signaled):
        try:
            os.kill(pid, 0)
        except OSError as e:
            if e.errno == errno.ESRCH:
                continue
        # Still alive — escalate.
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError as e:
            if e.errno == errno.ESRCH:
                continue
            log.warning(
                "sweep_and_kill_reparented_orphans: SIGKILL pid=%d failed: %s",
                pid,
                e,
            )
    return signaled
