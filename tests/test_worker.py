"""Unit tests for the worker — pure functions + cancel-signal e2e with a real subprocess."""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "worker"))

from job_worker import compute_unregistered_vram, pick_resource_snapshot_mock, run_job  # noqa: E402


def test_compute_unregistered_zero_when_all_tracked():
    nvidia_procs = [(1000, 4096), (1001, 2048)]  # (pid, MiB)
    tracked_pids = {1000, 1001}
    assert compute_unregistered_vram(nvidia_procs, tracked_pids) == 0.0


def test_compute_unregistered_counts_untracked():
    nvidia_procs = [(1000, 4096), (9999, 8192)]
    tracked_pids = {1000}
    mib = 8192
    expected_gb = round(mib / 1024.0, 2)
    assert compute_unregistered_vram(nvidia_procs, tracked_pids) == expected_gb


def test_pick_resource_snapshot_mock_shape():
    snap = pick_resource_snapshot_mock()
    for key in (
        "host",
        "free_vram_gb",
        "unregistered_vram_gb",
        "free_ram_gb",
        "idle_cpus",
        "host_aliases",
        "arch",
        "os",
        "gpu",
        "tags",
    ):
        assert key in snap


class _FakeResp:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data or {}

    def json(self):
        return self._data


class _FakeClient:
    """Minimal stand-in for httpx.Client. Records POSTs, answers GETs.

    cancel_after_s: wall-time elapsed from construction before /signal
    endpoints return {"signal": "cancel"}.
    """

    def __init__(self, cancel_after_s: float):
        self.cancel_after_s = cancel_after_s
        self.t0 = time.monotonic()
        self.posts: list[tuple[str, dict]] = []
        self.lock = threading.Lock()

    def get(self, path, **kw):
        if path.endswith("/signal"):
            elapsed = time.monotonic() - self.t0
            if elapsed >= self.cancel_after_s:
                return _FakeResp(200, {"signal": "cancel"})
            return _FakeResp(200, {"signal": None})
        return _FakeResp(200, {})

    def post(self, path, json=None, content=None, **kw):
        with self.lock:
            self.posts.append((path, json if json is not None else {"_raw": len(content or b"")}))
        return _FakeResp(200, {})


def test_run_job_cancel_signal_terminates_child(tmp_path, monkeypatch):
    """run_job must honor /signal=cancel: SIGTERM the child, exit with
    nonzero rc, and POST /complete with final_state=cancelled.

    Real subprocess (bash -c sleep 10); fake client returns cancel after
    1s. Whole test should finish in ~2s, well under the 10s sleep and
    the 60s grace window.

    Bypass heavy-run: the wrapper uses systemd-run --scope on cgroups
    whose signal propagation is environment-dependent and would make
    this test flaky on CI. Here we care about the signal chain inside
    run_job, not the heavy-run integration.
    """
    import job_worker

    monkeypatch.setattr(job_worker.shutil, "which", lambda _name: None)

    job = {"id": 9001, "cmd": ["bash", "-c", "sleep 10"], "cwd": str(tmp_path)}
    client = _FakeClient(cancel_after_s=1.0)
    tracked: set[int] = set()

    t0 = time.monotonic()
    run_job(client, job, tracked)
    elapsed = time.monotonic() - t0

    assert elapsed < 8.0, f"run_job should exit quickly after cancel, took {elapsed:.1f}s"
    assert not tracked, "pid should be discarded after completion"

    completes = [p for p in client.posts if p[0].endswith("/complete")]
    assert len(completes) == 1, f"expected one /complete, got {client.posts}"
    body = completes[0][1]
    assert body["final_state"] == "cancelled"
    assert body["exit_code"] != 0, f"cancelled process should have nonzero rc, got {body}"
