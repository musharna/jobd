"""S4 (runtime-zombies audit): subreaper + cgroup-walk paired reaper.

Pattern from Ray's raylet subreaper. Subreaper alone is insufficient
because PR_SET_CHILD_SUBREAPER does not propagate across
`systemd-run --scope` (verified during the 2026-05-18 OSS survey).
Cgroup-walk complements it: any PID still living in the per-job scope
cgroup after job completion gets SIGKILLed.

Motivated by the desktop orphan inference_server (PPID=1, 21h51m old,
GPU context still held, launcher long dead).
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

import pytest

from jobd import cgroup_walk, subreaper


def test_set_child_subreaper_on_linux_returns_true():
    """On Linux, prctl(PR_SET_CHILD_SUBREAPER, 1) must succeed."""
    if sys.platform != "linux":
        pytest.skip("subreaper only meaningful on Linux")
    ok = subreaper.set_child_subreaper()
    assert ok is True


def test_set_child_subreaper_is_idempotent():
    """Calling twice is safe (just re-sets the flag)."""
    if sys.platform != "linux":
        pytest.skip("subreaper only meaningful on Linux")
    assert subreaper.set_child_subreaper() is True
    assert subreaper.set_child_subreaper() is True


def test_set_child_subreaper_non_linux_returns_false(monkeypatch):
    """On non-Linux, the call no-ops with a warning and returns False
    instead of crashing the worker."""
    monkeypatch.setattr(subreaper, "_PLATFORM", "darwin")
    assert subreaper.set_child_subreaper() is False


def test_cgroup_walk_returns_empty_for_missing_scope(tmp_path):
    """Missing scope path → empty list, not an exception."""
    pids = cgroup_walk.list_scope_pids(tmp_path / "nonexistent.scope")
    assert pids == []


def test_cgroup_walk_reads_cgroup_procs(tmp_path):
    """Given a directory with a cgroup.procs file, walk returns the
    PIDs listed there (mocking the cgroup filesystem layout)."""
    scope_dir = tmp_path / "jobd-42.scope"
    scope_dir.mkdir()
    (scope_dir / "cgroup.procs").write_text("1234\n5678\n")
    pids = cgroup_walk.list_scope_pids(scope_dir)
    assert set(pids) == {1234, 5678}


def test_cgroup_walk_ignores_blank_and_invalid_lines(tmp_path):
    scope_dir = tmp_path / "jobd-7.scope"
    scope_dir.mkdir()
    (scope_dir / "cgroup.procs").write_text("100\n\nnot-a-pid\n200\n")
    pids = cgroup_walk.list_scope_pids(scope_dir)
    assert set(pids) == {100, 200}


def test_kill_scope_no_op_on_missing_pids(tmp_path):
    """kill_scope must tolerate PIDs that have already exited (ESRCH)."""
    scope_dir = tmp_path / "jobd-99.scope"
    scope_dir.mkdir()
    # Use a definitely-not-running PID range.
    (scope_dir / "cgroup.procs").write_text("9999999\n")
    killed = cgroup_walk.kill_scope(scope_dir)
    # Returns the PIDs it ATTEMPTED to kill — even if ESRCH'd.
    assert killed == [9999999]


def test_kill_scope_returns_empty_for_missing_scope(tmp_path):
    killed = cgroup_walk.kill_scope(tmp_path / "nonexistent.scope")
    assert killed == []


def test_locate_user_scope_path_resolves_unit_name():
    """resolve_user_scope_path is best-effort: returns a Path under the
    user cgroup or None when the cgroup filesystem isn't present."""
    p = cgroup_walk.resolve_user_scope_path("jobd-1.scope")
    # Either we find a path or get None — never raise.
    assert p is None or isinstance(p, Path)


def test_sweep_reparented_orphans_synthetic(monkeypatch):
    """S4 spec-review (missing /proc sweep): mock /proc + stat reads and
    verify the ppid==my_pid filter, the set-difference against
    known_pids, and that non-numeric /proc entries are skipped.
    """

    # Force the linux/proc gate even if test host disagrees with the
    # synthetic-fixture path (the function is otherwise guarded).
    monkeypatch.setattr(subreaper, "_PLATFORM", "linux")
    monkeypatch.setattr(subreaper.os.path, "isdir", lambda p: p == "/proc")

    # Fake getpid → 1000 so ppid==1000 means "reparented to us".
    monkeypatch.setattr(subreaper.os, "getpid", lambda: 1000)

    # /proc entries: real PIDs 1001-1004 + noise entries that must be skipped.
    monkeypatch.setattr(
        subreaper.os,
        "listdir",
        lambda p: ["1001", "1002", "1003", "1004", "1000", "self", "kmsg"],
    )

    fake_stat = {
        # pid 1001: ppid=1000, our orphan
        "/proc/1001/stat": "1001 (worker_child) S 1000 0 0 0 -1 0 0",
        # pid 1002: ppid=1000 but in known_pids — skipped
        "/proc/1002/stat": "1002 (job_pid) R 1000 0 0 0 -1 0 0",
        # pid 1003: ppid=2 (kthreadd) — not ours
        "/proc/1003/stat": "1003 (some_other) S 2 0 0 0 -1 0 0",
        # pid 1004: ppid=1000, second orphan; comm has spaces+paren
        "/proc/1004/stat": "1004 (weird (proc) name) S 1000 0 0 0 -1 0 0",
        # pid 1000: our own pid, filtered by the my_pid==pid skip
        "/proc/1000/stat": "1000 (self) S 1 0 0 0 -1 0 0",
    }

    real_open = open

    def fake_open(path, *a, **kw):
        if isinstance(path, str) and path.startswith("/proc/") and path.endswith("/stat"):
            content = fake_stat.get(path)
            if content is None:
                raise OSError("ENOENT")
            import io

            return io.StringIO(content)
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", fake_open)

    out = subreaper.sweep_reparented_orphans(known_pids={1002})
    # Only 1001 and 1004 should remain (1002 filtered by known_pids;
    # 1003 not our child; 1000 is our own pid; 'self'/'kmsg' non-numeric).
    assert out == [1001, 1004], out


def test_sweep_reparented_orphans_real_fork():
    """Real-execution check: fork a real subprocess, leave it sleeping,
    verify sweep_reparented_orphans({}) surfaces it. Cleans up the child
    before assertion fails."""
    if sys.platform != "linux":
        pytest.skip("real-fork sweep test is Linux-only")
    import os as _os
    import signal as _signal
    import time as _time

    pid = _os.fork()
    if pid == 0:
        # Child: sleep so the parent can observe us alive in /proc.
        try:
            _time.sleep(5)
        finally:
            _os._exit(0)

    try:
        # Give the kernel a tick to update /proc/<pid>/stat.
        _time.sleep(0.05)
        out = subreaper.sweep_reparented_orphans(known_pids=set())
        assert pid in out, f"child pid {pid} not surfaced by sweep; got {out}"
        # Sanity: with the child in known_pids, it is filtered out.
        out2 = subreaper.sweep_reparented_orphans(known_pids={pid})
        assert pid not in out2
    finally:
        # Clean up: SIGKILL + waitpid so we don't leak a zombie.
        with contextlib.suppress(OSError):
            _os.kill(pid, _signal.SIGKILL)
        with contextlib.suppress(OSError):
            _os.waitpid(pid, 0)
