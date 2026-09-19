"""Real-execution check for the startup sweep's ownership rule.

Two real `systemd-run --user --scope sleep` units, one in this worker's
namespace and one in another broker's. The sweep must kill the first and leave
the second running.

This runs on developer machines that may have a production worker's jobs alive
in the same user session, so the kill paths are interlocked: a unit this test
did not create is never killed, it is recorded, and the test fails. A
regression in the ownership rule therefore fails here rather than taking real
jobs down.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

import jobd.worker.job_worker as job_worker
from tests.unit.test_subreaper_real_execution import _has_systemd_user_scope

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not _has_systemd_user_scope(),
    reason="needs systemd-run --user on Linux with cgroup v2",
)


def _start_scope(unit: str) -> subprocess.Popen:
    return subprocess.Popen(
        ["systemd-run", "--user", "--scope", "--quiet", f"--unit={unit}", "sleep", "300"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _active(unit: str) -> bool:
    out = subprocess.run(
        ["systemctl", "--user", "is-active", unit], capture_output=True, text=True, timeout=10
    )
    return out.stdout.strip() == "active"


def _wait(predicate, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


def test_sweep_kills_its_own_stale_scope_and_not_another_brokers(monkeypatch):
    tag = os.getpid()
    job_worker.set_scope_namespace(f"http://sweep-test-own.example:{tag}")
    mine = job_worker.scope_unit_name(990000001)
    foreign_ns = job_worker.scope_namespace(f"http://sweep-test-foreign.example:{tag}")
    foreign = f"jobd-{foreign_ns}-990000001.scope"
    created = {mine, foreign}

    refused: list[str] = []
    real_kill_scope = job_worker._cgroup_walk.kill_scope
    real_run = subprocess.run

    def guarded_kill_scope(path):
        unit = os.path.basename(str(path))
        if unit not in created:
            refused.append(unit)
            return []
        return real_kill_scope(path)

    def guarded_run(args, *a, **kw):
        if "kill" in args and args[-1] not in created:
            refused.append(args[-1])
            return subprocess.CompletedProcess(args, 0, "", "")
        return real_run(args, *a, **kw)

    procs = [_start_scope(mine), _start_scope(foreign)]
    try:
        assert _wait(lambda: _active(mine) and _active(foreign)), "test scopes did not start"
        monkeypatch.setattr(job_worker._cgroup_walk, "kill_scope", guarded_kill_scope)
        monkeypatch.setattr(job_worker.subprocess, "run", guarded_run)

        swept = job_worker._sweep_stale_scopes()

        assert swept == [mine]
        assert _wait(lambda: not _active(mine)), "own stale scope survived the sweep"
        assert _active(foreign), "another broker's scope was killed"
        assert refused == [], f"the sweep tried to kill units it does not own: {refused}"
    finally:
        monkeypatch.undo()
        for unit in created:
            subprocess.run(
                ["systemctl", "--user", "kill", "--signal=KILL", unit],
                capture_output=True,
                timeout=10,
                check=False,
            )
        for p in procs:
            p.wait(timeout=10)
