"""S4 real-execution check: cgroup_walk against a real systemd-run --user
scope on this laptop.

NOT a synthetic-cgroup-fs mock. Spawns `systemd-run --user --scope sleep`,
walks the live cgroup, verifies the sleep PID appears, kills, verifies
gone. Skips cleanly if systemd-run --user isn't available (CI, macOS,
etc.).

Motivated by the user CLAUDE.md rule "Real-execution check at every
system boundary." Cgroup-fs paths are exactly the kind of boundary where
a synthetic-fixture pass leaves the real-deploy failure mode (wrong
cgroup-v2 layout, missing user@.service, etc.) undetected.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid

import pytest

from jobd import cgroup_walk


def _has_systemd_user_scope() -> bool:
    if sys.platform != "linux":
        return False
    if shutil.which("systemd-run") is None:
        return False
    # Probe: does `systemd-run --user --version` (no scope) at least exec?
    try:
        r = subprocess.run(
            ["systemd-run", "--user", "--version"],
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


@pytest.mark.skipif(
    not _has_systemd_user_scope(),
    reason="needs systemd-run --user on Linux with cgroup v2",
)
def test_cgroup_walk_finds_and_kills_live_scope_pid():
    """Spawn a real sleep inside a real --user scope; cgroup_walk finds
    the PID and kill_scope reaps it."""
    unit = f"jobd-s4rxc-{uuid.uuid4().hex[:8]}.scope"
    proc = subprocess.Popen(
        [
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            f"--unit={unit}",
            "sleep",
            "30",
        ]
    )

    try:
        # Wait briefly for systemd to set up the scope cgroup.
        path = None
        for _ in range(20):
            path = cgroup_walk.resolve_user_scope_path(unit)
            if path is not None and (path / "cgroup.procs").exists():
                break
            time.sleep(0.1)

        assert path is not None, f"could not resolve scope path for {unit}"
        pids = cgroup_walk.list_scope_pids(path)
        assert pids, f"scope {unit} had no PIDs"
        # sleep PID should be in there.
        # (Don't assert proc.pid directly — systemd-run client exits;
        # the sleep is a child inside the scope.)
        # We just need at least one PID, and a kill_scope call to land.
        reaped = cgroup_walk.kill_scope(path)
        assert reaped, "kill_scope returned empty list"

        # Give the kernel a moment to reap.
        for _ in range(20):
            pids2 = cgroup_walk.list_scope_pids(path)
            if not pids2:
                break
            time.sleep(0.1)
        # After SIGKILL the scope's PIDs should be gone.
        # (cgroup itself may linger briefly until systemd cleans up.)
        assert cgroup_walk.list_scope_pids(path) == [] or all(
            _proc_dead(p) for p in cgroup_walk.list_scope_pids(path)
        )
    finally:
        # Defensive: belt-and-suspenders cleanup if anything escaped.
        try:
            subprocess.run(
                ["systemctl", "--user", "stop", unit],
                check=False,
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def _proc_dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        return False


@pytest.mark.skipif(sys.platform != "linux", reason="prctl is Linux-only")
def test_set_child_subreaper_real_call_does_not_raise():
    """Live prctl call against the current process. This test pollutes the
    test runner's subreaper bit, but that's harmless — it just means any
    orphan child of the test process would reparent here instead of init.
    """
    from jobd import subreaper

    assert subreaper.set_child_subreaper() is True
