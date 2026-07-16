"""SIGTERM drain (docs/plans/sigterm-drain.md Phase 1).

The worker's shutdown path must signal in-flight workloads through run_job's
preempt machinery, join job threads with a bounded deadline, and post a
summary event — instead of sys.exit(0)-ing daemon threads mid-job. The signal
handler itself must do nothing but set the stop flag (re-entering the shared
httpx client from a handler can deadlock on its pool lock).
"""

import signal
import subprocess
import threading
import time

import pytest

import jobd.worker.job_worker as job_worker


class _FakeResp:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data or {}

    def json(self):
        return self._data


class _FakeClient:
    """Records POSTs; /signal always answers 'no signal' (drain is the only
    termination source in these tests)."""

    def __init__(self):
        self.posts: list[tuple[str, object]] = []
        self.lock = threading.Lock()

    def get(self, path, **kw):
        if path.endswith("/signal"):
            return _FakeResp(200, {"signal": None})
        return _FakeResp(200, {})

    def post(self, path, json=None, content=None, **kw):
        with self.lock:
            self.posts.append((path, json if json is not None else (content or b"")))
        return _FakeResp(200, {})

    def completes(self):
        return [
            p
            for p in self.posts
            if p[0].endswith("/complete") and not p[0].endswith("/checkpoint-complete")
        ]


@pytest.fixture(autouse=True)
def _clean_drain_state():
    job_worker._drain_event.clear()
    with job_worker._drain_hooks_lock:
        job_worker._drain_hooks.clear()
    yield
    job_worker._drain_event.clear()
    with job_worker._drain_hooks_lock:
        job_worker._drain_hooks.clear()


def _wait_for_hook(job_id: int, timeout: float = 5.0):
    """Poll until run_job registers its drain hook; return the hook."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with job_worker._drain_hooks_lock:
            hook = job_worker._drain_hooks.get(job_id)
        if hook is not None:
            return hook
        time.sleep(0.02)
    raise AssertionError(f"drain hook for job {job_id} never registered")


# ---- signal handler ------------------------------------------------------


def test_shutdown_handler_only_sets_stop_event():
    """The handler must not touch the httpx client (pool-lock deadlock) and
    must not sys.exit (kills daemon job threads without cleanup). Flag only."""
    ev = threading.Event()
    handler = job_worker._make_shutdown_handler(ev)
    handler(signal.SIGTERM, None)  # must not raise (SystemExit included)
    assert ev.is_set()


# ---- drain grace knob ----------------------------------------------------


def test_drain_grace_default_and_env_override(monkeypatch):
    monkeypatch.delenv("JOBD_WORKER_DRAIN_GRACE_S", raising=False)
    assert job_worker._drain_grace_s() == 60.0
    monkeypatch.setenv("JOBD_WORKER_DRAIN_GRACE_S", "120")
    assert job_worker._drain_grace_s() == 120.0
    monkeypatch.setenv("JOBD_WORKER_DRAIN_GRACE_S", "garbage")
    assert job_worker._drain_grace_s() == 60.0


# ---- drain_in_flight -----------------------------------------------------


def test_drain_invokes_every_hook_with_reason_and_grace():
    calls: list[tuple[int, str, float]] = []
    job_worker._register_drain_hook(1, lambda reason, grace: calls.append((1, reason, grace)))
    job_worker._register_drain_hook(2, lambda reason, grace: calls.append((2, reason, grace)))
    summary = job_worker.drain_in_flight([], grace_s=7.0, join_margin_s=0.1)
    assert sorted(calls) == [(1, "worker_shutdown", 7.0), (2, "worker_shutdown", 7.0)]
    assert summary["signaled"] == 2
    assert summary["aborted"] == 0


def test_drain_sets_draining_flag_before_invoking_hooks():
    """run_job's gap-race check reads _drain_event after registering its hook;
    the flag must already be set when hooks fire or a job dispatched mid-drain
    could miss both the hook call and the flag."""
    seen: list[bool] = []
    job_worker._register_drain_hook(5, lambda _r, _g: seen.append(job_worker._drain_event.is_set()))
    job_worker.drain_in_flight([], grace_s=1.0, join_margin_s=0.1)
    assert seen == [True]


def test_drain_survives_a_hook_that_raises():
    calls: list[int] = []

    def bad_hook(_r, _g):
        raise RuntimeError("boom")

    job_worker._register_drain_hook(1, bad_hook)
    job_worker._register_drain_hook(2, lambda _r, _g: calls.append(2))
    summary = job_worker.drain_in_flight([], grace_s=1.0, join_margin_s=0.1)
    assert calls == [2]
    assert summary["signaled"] == 2  # both attempted


def test_drain_joins_threads_and_counts_abandoned():
    finished = threading.Event()
    wedged = threading.Event()
    t_done = threading.Thread(target=lambda: finished.wait(5), daemon=True)
    t_hang = threading.Thread(target=lambda: wedged.wait(30), daemon=True)
    t_done.start()
    t_hang.start()
    finished.set()
    summary = job_worker.drain_in_flight([t_done, t_hang], grace_s=0.1, join_margin_s=0.3)
    assert summary["aborted"] == 1
    wedged.set()


# ---- run_job integration -------------------------------------------------


def test_run_job_refuses_new_work_when_draining(tmp_path, monkeypatch):
    """A job dispatched after the drain flag is set must never Popen — it
    completes immediately as preempted/worker_shutdown so the broker record
    doesn't strand in ASSIGNED."""
    monkeypatch.setattr(job_worker.shutil, "which", lambda _n: None)

    def _no_popen(*_a, **_kw):
        raise AssertionError("must not start a subprocess while draining")

    monkeypatch.setattr(subprocess, "Popen", _no_popen)
    job_worker._drain_event.set()

    client = _FakeClient()
    job_worker.run_job(client, {"id": 7000, "cmd": ["echo", "hi"], "cwd": str(tmp_path)}, set())

    assert not any(p[0].endswith("/started") for p in client.posts)
    completes = client.completes()
    assert len(completes) == 1
    body = completes[0][1]
    assert body["final_state"] == "preempted"
    assert body["termination_reason"] == "worker_shutdown"
    assert body["exit_code"] is None


def test_drain_hook_registered_during_run_and_removed_after(tmp_path, monkeypatch):
    monkeypatch.setattr(job_worker.shutil, "which", lambda _n: None)
    release = threading.Event()

    class _StubStdout:
        def read(self, _n):
            release.wait(timeout=10.0)
            return b""

        def close(self):
            pass

    class _StubProc:
        def __init__(self, _cmd, **_kw):
            self.pid = 44444
            self.stdout = _StubStdout()

        def send_signal(self, _sig):
            release.set()

        def wait(self, timeout=None):
            release.wait(timeout=10.0)
            return 0

        def kill(self):
            release.set()

        def poll(self):
            return 0 if release.is_set() else None

    monkeypatch.setattr(subprocess, "Popen", _StubProc)
    client = _FakeClient()
    job = {"id": 8001, "cmd": ["bash", "-c", "sleep 30"], "cwd": str(tmp_path)}
    t = threading.Thread(target=job_worker.run_job, args=(client, job, set()), daemon=True)
    t.start()

    _wait_for_hook(8001)
    release.set()
    t.join(5)
    assert not t.is_alive()
    with job_worker._drain_hooks_lock:
        assert 8001 not in job_worker._drain_hooks


def test_drain_hook_preempts_running_job(tmp_path, monkeypatch):
    """The load-bearing case: a real running subprocess, drained via its hook,
    lands as preempted/worker_shutdown — not failed, not stranded RUNNING."""
    monkeypatch.setattr(job_worker.shutil, "which", lambda _n: None)
    client = _FakeClient()
    job = {"id": 8002, "cmd": ["bash", "-c", "sleep 30"], "cwd": str(tmp_path)}
    t = threading.Thread(target=job_worker.run_job, args=(client, job, set()), daemon=True)
    t0 = time.monotonic()
    t.start()

    hook = _wait_for_hook(8002)
    hook("worker_shutdown", 60.0)
    t.join(10)
    elapsed = time.monotonic() - t0

    assert not t.is_alive(), "run_job should finish promptly after drain TERM"
    assert elapsed < 8.0, f"drain should terminate the job quickly, took {elapsed:.1f}s"
    completes = client.completes()
    assert len(completes) == 1
    body = completes[0][1]
    assert body["final_state"] == "preempted"
    assert body["termination_reason"] == "worker_shutdown"


def test_drain_grace_cap_overrides_job_checkpoint_grace(tmp_path, monkeypatch):
    """A job with checkpoint_grace_s=300 must not pin the drain for 5 minutes:
    the effective grace is min(job grace, drain cap). The workload ignores
    SIGTERM, so only the capped SIGKILL escalation can end it."""
    monkeypatch.setattr(job_worker.shutil, "which", lambda _n: None)
    client = _FakeClient()
    job = {
        "id": 8003,
        "cmd": [
            "python3",
            "-c",
            "import signal,time\n"
            "signal.signal(signal.SIGTERM, lambda *a: None)\n"
            'print("READY", flush=True)\n'
            "time.sleep(30)\n",
        ],
        "cwd": str(tmp_path),
        "checkpoint_grace_s": 300,
    }
    t = threading.Thread(target=job_worker.run_job, args=(client, job, set()), daemon=True)
    t0 = time.monotonic()
    t.start()

    hook = _wait_for_hook(8003)
    # Wait for the workload's READY byte (streamed to /log) — proof its
    # SIGTERM-ignoring handler is installed. A fixed sleep raced a cold
    # python3 start: fire too early and the TERM ends the job within grace,
    # so the SIGKILL-cap path under test never executes (vacuous pass).
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if any(p[0].endswith("/log") and b"READY" in bytes(p[1]) for p in client.posts):
            break
        time.sleep(0.02)
    else:
        raise AssertionError("workload never printed READY")
    hook("worker_shutdown", 1.0)
    t.join(15)
    elapsed = time.monotonic() - t0

    assert not t.is_alive()
    assert elapsed < 10.0, f"grace cap of 1s should bound the drain, took {elapsed:.1f}s"
    completes = client.completes()
    assert len(completes) == 1
    assert completes[0][1]["final_state"] == "preempted"


def test_drain_starting_during_popen_still_terminates_job(tmp_path, monkeypatch):
    """Gap race: drain begins after _register_in_flight but before run_job
    registers its hook. run_job re-checks _drain_event right after
    registration and self-terminates."""
    monkeypatch.setattr(job_worker.shutil, "which", lambda _n: None)
    release = threading.Event()

    class _StubStdout:
        def read(self, _n):
            release.wait(timeout=10.0)
            return b""

        def close(self):
            pass

    class _StubProc:
        def __init__(self, _cmd, **_kw):
            # Drain starts while Popen is in progress — before the hook exists.
            job_worker._drain_event.set()
            self.pid = 55555
            self.stdout = _StubStdout()

        def send_signal(self, _sig):
            release.set()

        def wait(self, timeout=None):
            release.wait(timeout=10.0)
            return -15

        def kill(self):
            release.set()

        def poll(self):
            return -15 if release.is_set() else None

    monkeypatch.setattr(subprocess, "Popen", _StubProc)
    client = _FakeClient()
    job = {"id": 8004, "cmd": ["bash", "-c", "sleep 30"], "cwd": str(tmp_path)}
    job_worker.run_job(client, job, set())  # must return, not hang

    completes = client.completes()
    assert len(completes) == 1
    body = completes[0][1]
    assert body["final_state"] == "preempted"
    assert body["termination_reason"] == "worker_shutdown"


# ---- main() wiring -------------------------------------------------------


def test_graceful_shutdown_drains_and_posts_summary_event(monkeypatch):
    monkeypatch.delenv("JOBD_WORKER_DRAIN_GRACE_S", raising=False)
    client = _FakeClient()
    calls: list[tuple[str, float]] = []
    job_worker._register_drain_hook(1, lambda r, g: calls.append((r, g)))

    summary = job_worker._graceful_shutdown(client, [])

    assert calls == [("worker_shutdown", 60.0)]
    assert summary["signaled"] == 1
    events = [p[1] for p in client.posts if p[0] == "/events"]
    shutdown_events = [e for e in events if e.get("event") == "worker_shutdown"]
    assert len(shutdown_events) == 1
    assert shutdown_events[0]["payload"]["signaled"] == 1
    assert shutdown_events[0]["payload"]["aborted"] == 0
