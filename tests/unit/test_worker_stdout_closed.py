"""A workload that closes its stdout is still cancellable and still wall-capped.

`stream_output` used to set `stop_signal` at stdout EOF, which ended
`poll_signals` — the loop that polls GET /signal and runs the wall-clock
watchdog. A workload that redirects its own output (`exec >log 2>&1`, a daemon
that closes fds) hits EOF at once, so from then on a user cancel was never seen
and max_wall_s never fired, while the process kept running.

EOF is not exit: the poll now stops when the process exits. After EOF the two
output-based watchdogs (idle, first-output) disarm, because a closed stdout can
never produce output again and silence there is not evidence of a hang.

Real subprocesses; systemd-run is bypassed (`shutil.which` -> None), as in
tests/test_worker.py, so signals go straight to the child.
"""

from __future__ import annotations

import threading
import time

from jobd.worker.job_worker import run_job

_CLOSES_STDOUT = "exec >/dev/null 2>&1; sleep 10"


class _Resp:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data or {}

    def json(self):
        return self._data


class _Client:
    """Records POSTs; /signal answers cancel once cancel_after_s has passed."""

    def __init__(self, cancel_after_s: float):
        self.cancel_after_s = cancel_after_s
        self.t0 = time.monotonic()
        self.posts: list[tuple[str, dict]] = []
        self.lock = threading.Lock()

    def get(self, path, **kw):
        if path.endswith("/signal") and time.monotonic() - self.t0 >= self.cancel_after_s:
            return _Resp(200, {"signal": "cancel"})
        return _Resp(200, {"signal": None})

    def post(self, path, json=None, content=None, **kw):
        with self.lock:
            self.posts.append((path, json if json is not None else {}))
        return _Resp(200, {})

    def complete(self) -> dict:
        done = [b for p, b in self.posts if p.endswith("/complete")]
        assert len(done) == 1, self.posts
        return done[0]


def _bypass_scope(monkeypatch):
    import jobd.worker.job_worker as job_worker

    monkeypatch.setattr(job_worker.shutil, "which", lambda _name: None)
    monkeypatch.setattr(job_worker, "SIGNAL_POLL_INTERVAL_S", 0.1)


def test_a_cancel_reaches_a_workload_that_closed_its_stdout(tmp_path, monkeypatch):
    _bypass_scope(monkeypatch)
    client = _Client(cancel_after_s=1.0)
    job = {"id": 9501, "cmd": ["bash", "-c", _CLOSES_STDOUT], "cwd": str(tmp_path)}

    t0 = time.monotonic()
    run_job(client, job, set())
    elapsed = time.monotonic() - t0

    assert elapsed < 6.0, (
        f"the cancel was never delivered: the job ran its full 10 s ({elapsed:.1f}s) "
        "because the signal poll stopped at stdout EOF"
    )
    assert client.complete()["final_state"] == "cancelled"


def test_max_wall_still_fires_after_stdout_closes(tmp_path, monkeypatch):
    _bypass_scope(monkeypatch)
    client = _Client(cancel_after_s=999.0)
    job = {
        "id": 9502,
        "cmd": ["bash", "-c", _CLOSES_STDOUT],
        "cwd": str(tmp_path),
        "max_wall_s": 1,
    }

    t0 = time.monotonic()
    run_job(client, job, set())
    elapsed = time.monotonic() - t0

    assert elapsed < 6.0, f"max_wall_s=1 never fired ({elapsed:.1f}s)"
    body = client.complete()
    assert body["final_state"] == "failed"
    assert body["termination_reason"] == "wall_timeout"


def test_output_watchdogs_do_not_kill_a_quiet_job_after_stdout_closes(tmp_path, monkeypatch):
    """Control: with polling kept alive past EOF, idle_timeout and the
    first-output watchdog must not mistake a closed stdout for a hang."""
    _bypass_scope(monkeypatch)
    monkeypatch.setenv("JOBD_WORKER_FIRST_OUTPUT_TIMEOUT_S", "1")
    client = _Client(cancel_after_s=999.0)
    job = {
        "id": 9503,
        "cmd": ["bash", "-c", "exec >/dev/null 2>&1; sleep 3"],
        "cwd": str(tmp_path),
        "idle_timeout_s": 1,
    }

    run_job(client, job, set())

    body = client.complete()
    assert body["final_state"] == "completed", body
    assert body["exit_code"] == 0


def test_every_report_carries_the_claims_dispatch_seq(tmp_path, monkeypatch):
    """The worker side of the dispatch fence (tests/unit/test_dispatch_fence.py):
    a job handed out with dispatch_seq echoes it on /started and /complete."""
    _bypass_scope(monkeypatch)
    seen: list[tuple[str, dict]] = []

    class _HeaderClient(_Client):
        def post(self, path, json=None, content=None, headers=None, **kw):
            seen.append((path, dict(headers or {})))
            return super().post(path, json=json, content=content, **kw)

    job = {"id": 9504, "cmd": ["bash", "-c", "echo hi"], "cwd": str(tmp_path), "dispatch_seq": 7}
    run_job(_HeaderClient(cancel_after_s=999.0), job, set())

    reports = [(p, h) for p, h in seen if p.rsplit("/", 1)[-1] in ("started", "log", "complete")]
    assert {p.rsplit("/", 1)[-1] for p, _ in reports} >= {"started", "complete"}, seen
    assert all(h.get("X-Jobd-Dispatch") == "7" for _, h in reports), reports
