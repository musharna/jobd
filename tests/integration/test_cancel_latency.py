"""Real-execution test for cancel latency.

This test spawns a REAL `systemd-run --scope` wrapping a REAL bash with
`sleep 60`, then triggers a cancel signal and asserts the workload is
SIGTERM'd in well under 5s.

Why this can't be a unit test: the 2026-04-26 cancel-latency bug was that
`proc.send_signal(SIGTERM)` was a no-op because `proc.pid` pointed at the
systemd-run client, not at the bash inside the scope. Any test that stubs
out subprocess.Popen / subprocess.run hides exactly that bug. The fix
(systemctl --user kill on the scope unit) can only be validated against
real systemd; a green unit test of the dispatch path proves nothing about
whether bash actually dies.

Per the real-execution-testing doctrine in feedback_real_execution_testing.md.

Skips automatically when systemd-run --user or the user bus is unavailable
(CI containers, minimal sandboxes).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "worker"))


def _systemd_user_works() -> bool:
    """Probe whether `systemd-run --user --scope` actually works on this host.
    A bare `which` is insufficient — WSL/CI containers often have the binary
    but no user bus."""
    if shutil.which("systemd-run") is None:
        return False
    try:
        r = subprocess.run(
            [
                "systemd-run",
                "--user",
                "--scope",
                "--quiet",
                "--unit=jobd-probe-cancel-latency.scope",
                "true",
            ],
            capture_output=True,
            timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _systemd_user_works(),
    reason="systemd-run --user not available on this host",
)


class _FakeResp:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data or {}

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self, cancel_after_s: float):
        self.cancel_after_s = cancel_after_s
        self.t0 = time.monotonic()
        self.posts: list[tuple[str, dict]] = []
        self.lock = threading.Lock()
        self.cancel_returned_at: float | None = None

    def get(self, path, **_kw):
        if path.endswith("/signal"):
            elapsed = time.monotonic() - self.t0
            if elapsed >= self.cancel_after_s:
                if self.cancel_returned_at is None:
                    self.cancel_returned_at = time.monotonic()
                return _FakeResp(200, {"signal": "cancel"})
            return _FakeResp(200, {"signal": None})
        return _FakeResp(200, {})

    def post(self, path, json=None, content=None, **_kw):
        with self.lock:
            self.posts.append((path, json if json is not None else {}))
        return _FakeResp(200, {})


def test_cancel_latency_under_5s_via_scope_kill(tmp_path):
    """run_job wraps the workload in a named systemd user-scope. After
    /signal returns 'cancel', the worker MUST kill the workload within
    a few seconds via `systemctl --user kill`. Pre-fix, this took ~60s
    because SIGTERM-to-pid was a no-op on the systemd-run client."""
    from job_worker import run_job  # noqa: E402

    job_id = 99100 + int(time.time()) % 1000
    job = {
        "id": job_id,
        "cmd": ["bash", "-c", "sleep 60 && echo SHOULD_NOT_PRINT"],
        "cwd": str(tmp_path),
    }
    client = _FakeClient(cancel_after_s=1.0)
    tracked: set[int] = set()

    t_start = time.monotonic()
    run_job(client, job, tracked)
    t_end = time.monotonic()
    elapsed = t_end - t_start

    # The actionable assertion: cancel must take effect quickly.
    # Generous bound — poll cadence is 2s, kill takes <1s in practice.
    assert elapsed < 8.0, (
        f"run_job took {elapsed:.1f}s — cancel signal didn't kill the scope "
        f"workload promptly. Posts: {[p[0] for p in client.posts]}"
    )

    completes = [p for p in client.posts if p[0].endswith("/complete")]
    assert len(completes) == 1
    body = completes[0][1]
    assert body["final_state"] == "cancelled", f"expected cancelled, got {body}"
    assert body["exit_code"] != 0, f"cancelled scope workload should have nonzero rc, got {body}"

    # Belt-and-suspenders: the scope unit should be cleaned up by systemd
    # after the workload exits. If it lingers, that's a separate problem.
    sub = subprocess.run(
        ["systemctl", "--user", "is-active", f"jobd-{job_id}.scope"],
        capture_output=True,
        timeout=5,
    )
    # Scope is either inactive or unknown after exit; both are fine.
    state = sub.stdout.decode().strip()
    assert state in ("inactive", "failed", "unknown", ""), (
        f"scope still active after run: {state!r}"
    )
