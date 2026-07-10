"""H-2 (audit 2026-07-10): run_job terminal path is exception-guarded.

If the post-Popen body raises (a proc.stdout.read() pipe/closed-file race, a
signal-thread start failure under load), the job must still be terminalized:
without the guard the job strands RUNNING, the tracked pid leaks (mis-counts
foreign VRAM), and the scope cgroup is never reaped. The fix wraps the body in
try/except/finally that always posts /complete(failed, worker_exec_error) and
discards the tracked pid when the body did not reach its own terminal post.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import jobd.worker.job_worker as job_worker


class _RaisingStdout:
    def read(self, _n):
        raise OSError("simulated pipe read failure")

    def close(self):
        pass


class _FakeProc:
    def __init__(self):
        self.pid = 424242
        self.stdout = _RaisingStdout()

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def send_signal(self, _s):
        pass


def _no_signal_client():
    client = MagicMock()

    def _get(path, **k):
        resp = MagicMock()
        resp.status_code = 404
        resp.json.return_value = {"signal": None}
        return resp

    client.get.side_effect = _get
    client.post.return_value = MagicMock()
    return client


def _complete_calls(client):
    return [c for c in client.post.call_args_list if str(c.args[0]).endswith("/complete")]


def test_run_job_body_raise_still_posts_failed_and_discards_pid(monkeypatch, tmp_path):
    fake_proc = _FakeProc()
    monkeypatch.setattr(job_worker.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(job_worker.shutil, "which", lambda _n: None)  # fast path, no scope
    monkeypatch.setenv("JOBD_WORKER_CHECKPOINT_ROOT", str(tmp_path))

    client = _no_signal_client()
    job = {"id": 777, "cmd": ["true"], "cwd": str(tmp_path), "fast_path": True}
    tracked: set[int] = set()

    # Must not propagate — the guard handles it.
    job_worker.run_job(client, job, tracked)

    # A terminal /complete(failed, worker_exec_error) was posted.
    completes = _complete_calls(client)
    assert completes, "run_job did not post /complete on body failure"
    payload = completes[-1].kwargs["json"]
    assert payload["final_state"] == "failed"
    assert payload["termination_reason"] == "worker_exec_error"

    # The tracked pid was discarded (no leak → no foreign-VRAM mis-count).
    assert fake_proc.pid not in tracked
