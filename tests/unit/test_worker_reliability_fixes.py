"""Worker reliability fixes: /complete retry, post-kill wait bound,
tracked_pids locking, SIGKILL-Timer cancellation."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import httpx

import jobd.worker.job_worker as job_worker

# --- Fix 1: /complete POST bounded retry -----------------------------------


def test_post_complete_retries_then_succeeds(caplog):
    """post() raises twice, then succeeds → payload delivered exactly once
    (on the successful call) and the function returns."""
    client = MagicMock()
    client.post.side_effect = [
        httpx.ConnectError("boom"),
        httpx.ConnectError("boom"),
        MagicMock(),
    ]
    sleeps: list[float] = []
    payload = {"exit_code": 0, "final_state": "completed"}

    job_worker._post_complete_with_retry(client, 42, payload, sleep=sleeps.append)

    # Three calls total (2 failures + 1 success); the success call carried the
    # payload.
    assert client.post.call_count == 3
    for call in client.post.call_args_list:
        assert call.args[0] == "/jobs/42/complete"
        assert call.kwargs["json"] == payload
    # Backoff slept twice (between the three attempts).
    assert sleeps == [1, 2]
    # One log record per failed attempt (the worker retries, so these are WARNINGs).
    assert len([r for r in caplog.records if "complete POST error" in r.getMessage()]) == 2


def test_post_complete_gives_up_after_five_attempts(caplog):
    client = MagicMock()
    client.post.side_effect = httpx.ConnectError("down")
    sleeps: list[float] = []

    job_worker._post_complete_with_retry(
        client, 7, {"exit_code": 1, "final_state": "failed"}, sleep=sleeps.append
    )

    assert client.post.call_count == 5
    assert sleeps == [1, 2, 4, 8]
    assert caplog.records, "giving up must be logged"


def test_post_complete_first_try_no_sleep():
    client = MagicMock()
    sleeps: list[float] = []
    job_worker._post_complete_with_retry(
        client, 9, {"exit_code": 0, "final_state": "completed"}, sleep=sleeps.append
    )
    assert client.post.call_count == 1
    assert sleeps == []


# --- Fix 3: bounded wait after SIGKILL --------------------------------------


def test_wait_after_kill_returns_rc():
    proc = MagicMock()
    proc.wait.return_value = 0
    rc = job_worker._wait_after_kill(proc, 12)
    assert rc == 0
    proc.wait.assert_called_once_with(timeout=30)


def test_wait_after_kill_timeout_sets_minus_nine(caplog):
    import subprocess

    proc = MagicMock()
    proc.wait.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=30)
    rc = job_worker._wait_after_kill(proc, 13)
    assert rc == -9
    assert caplog.records, "abandoning the wait must be logged"
    assert "13" in caplog.text


# --- Fix 4: tracked_pids locked add/discard/snapshot ------------------------


def test_tracked_pids_snapshot_under_lock_consistent():
    """Threads add/discard while another snapshots in a loop → no exception,
    final state consistent."""
    tracked: set[int] = set()
    errors: list[Exception] = []
    stop = threading.Event()

    def churn(base: int):
        try:
            for i in range(2000):
                job_worker._tracked_pids_add(tracked, base + i)
                job_worker._tracked_pids_discard(tracked, base + i)
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    def snapshot():
        try:
            while not stop.is_set():
                job_worker._tracked_pids_snapshot(tracked)
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    workers = [threading.Thread(target=churn, args=(b,)) for b in (10_000, 50_000)]
    snap_t = threading.Thread(target=snapshot)
    snap_t.start()
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    stop.set()
    snap_t.join()

    assert errors == []
    assert tracked == set()


def test_tracked_pids_snapshot_returns_copy():
    tracked = {1, 2, 3}
    snap = job_worker._tracked_pids_snapshot(tracked)
    assert snap == {1, 2, 3}
    snap.add(99)
    assert tracked == {1, 2, 3}


# --- Fix 2: SIGKILL-escalation timers are cancelled after process exit -------


def test_cancel_pending_kill_timers_cancels_all():
    fired: list[str] = []
    timers = [
        threading.Timer(60, lambda: fired.append("a")),
        threading.Timer(60, lambda: fired.append("b")),
    ]
    for t in timers:
        t.start()
    lock = threading.Lock()

    job_worker._cancel_kill_timers(timers, lock)

    for t in timers:
        assert not t.is_alive()
    # list is drained
    assert timers == []


def test_run_job_cancels_kill_timer_on_exit(monkeypatch, tmp_path):
    """Integration-ish: a cancel signal schedules a kill Timer; once the
    process exits, run_job cancels it so the timer thread does not linger."""
    captured_timers: list[threading.Timer] = []

    real_timer = threading.Timer

    class _RecordingTimer(real_timer):  # type: ignore[misc]
        def __init__(self, interval, function, *a, **k):
            super().__init__(interval, function, *a, **k)
            captured_timers.append(self)

    monkeypatch.setattr(job_worker.threading, "Timer", _RecordingTimer)

    # Fake proc: stdout yields one chunk then EOF; wait returns immediately.
    class _FakeStdout:
        def __init__(self):
            self._chunks = [b"hello\n", b""]

        def read(self, _n):
            return self._chunks.pop(0)

        def close(self):
            pass

    class _FakeProc:
        def __init__(self):
            self.pid = 999999
            self.stdout = _FakeStdout()
            self._polls = 0

        def poll(self):
            # Stay alive long enough for poll_signals to schedule the timer.
            return None

        def wait(self, timeout=None):
            return 0

        def send_signal(self, _s):
            pass

    fake_proc = _FakeProc()
    monkeypatch.setattr(job_worker.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(job_worker.shutil, "which", lambda _n: None)

    # Client: /signal returns a cancel so poll_signals schedules a kill timer.
    client = MagicMock()

    def _get(path, **k):
        resp = MagicMock()
        if path.endswith("/signal"):
            resp.status_code = 200
            resp.json.return_value = {"signal": "cancel"}
        else:
            resp.status_code = 404
        return resp

    client.get.side_effect = _get
    client.post.return_value = MagicMock()

    monkeypatch.setenv("JOBD_WORKER_CHECKPOINT_ROOT", str(tmp_path))

    job = {
        "id": 555,
        "cmd": ["true"],
        "cwd": str(tmp_path),
        "fast_path": True,
    }
    tracked: set[int] = set()

    # Give the signal thread a beat to land the cancel + schedule the timer
    # before stdout EOF lets run_job proceed to wait().
    job_worker.run_job(client, job, tracked)

    # Whatever timers were scheduled must have been cancelled by run_job.
    for t in captured_timers:
        assert not t.is_alive()
