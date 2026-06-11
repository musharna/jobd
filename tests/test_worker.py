"""Unit tests for the worker — pure functions + cancel-signal e2e with a real subprocess."""

import threading
import time

import jobd.worker.job_worker as job_worker
from jobd.worker.job_worker import (
    _missing_launcher_path,
    command_has_concurrent_ok,
    compute_unregistered_vram,
    effective_vram_request_gb_from_job,
    gpu_admission_check,
    pick_resource_snapshot_mock,
    run_job,
)


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


def _stub_nvml(monkeypatch, free_gb: float, procs: list[tuple[int, int]]):
    """Force NVML on and stub the two callable seams."""
    monkeypatch.setattr(job_worker, "_NVML_OK", True)
    monkeypatch.setattr(job_worker, "nvidia_free_vram_gb", lambda: free_gb)
    monkeypatch.setattr(job_worker, "nvidia_processes", lambda: procs)


def test_admission_check_no_gpu_required_returns_unblocked(monkeypatch):
    """required_gb<=0 short-circuits — non-GPU jobs don't gate on this."""
    monkeypatch.setattr(job_worker, "_NVML_OK", True)
    out = gpu_admission_check(0.0, tracked_pids=set())
    assert out["blocked"] is False
    assert out["required_gb"] == 0.0


def test_admission_check_nvml_unavailable_returns_unblocked(monkeypatch):
    """Hosts without NVML (e.g. server CPU-only) skip the gate cleanly."""
    monkeypatch.setattr(job_worker, "_NVML_OK", False)
    out = gpu_admission_check(8.0, tracked_pids=set())
    assert out["blocked"] is False
    assert out["foreign_pids"] == []


def test_admission_check_unblocked_when_free_meets_required(monkeypatch):
    _stub_nvml(monkeypatch, free_gb=12.0, procs=[(1000, 4096)])
    out = gpu_admission_check(8.0, tracked_pids={1000})
    assert out["blocked"] is False
    assert out["free_gb"] == 12.0
    assert out["required_gb"] == 8.0
    assert out["foreign_pids"] == []  # 1000 is owned by us
    assert out["foreign_vram_gb"] == 0.0


def test_admission_check_blocked_when_foreign_holds_vram(monkeypatch):
    """Foreign holder pushes free below required — the load-bearing case."""
    _stub_nvml(monkeypatch, free_gb=2.0, procs=[(1000, 4096), (9999, 10240)])
    out = gpu_admission_check(8.0, tracked_pids={1000})
    assert out["blocked"] is True
    assert out["free_gb"] == 2.0
    assert out["required_gb"] == 8.0
    assert out["foreign_pids"] == [9999]
    assert out["foreign_vram_gb"] == 10.0


def test_admission_check_foreign_diagnostics_even_when_unblocked(monkeypatch):
    """A foreign holder that doesn't push free below required: blocked=False
    but the diagnostic fields still surface the foreign PID — useful for
    co-routing telemetry later (#42).
    """
    _stub_nvml(monkeypatch, free_gb=20.0, procs=[(9999, 8192)])
    out = gpu_admission_check(8.0, tracked_pids=set())
    assert out["blocked"] is False
    assert out["foreign_pids"] == [9999]
    assert out["foreign_vram_gb"] == 8.0


def test_admission_check_sums_multiple_foreign_holders(monkeypatch):
    _stub_nvml(monkeypatch, free_gb=1.0, procs=[(8001, 5120), (8002, 6144)])
    out = gpu_admission_check(8.0, tracked_pids=set())
    assert out["blocked"] is True
    assert out["foreign_pids"] == [8001, 8002]  # sorted
    assert out["foreign_vram_gb"] == 11.0


def test_effective_vram_explicit_wins():
    """#41d worker mirror: `vram_gb` overrides any tier-tag / gpu fallback."""
    job = {"vram_gb": 16.0, "requires": {"gpu": True, "needs": ["cuda-8gb"]}}
    assert effective_vram_request_gb_from_job(job) == 16.0


def test_effective_vram_tier_tag_picks_max():
    job = {"requires": {"gpu": True, "needs": ["cuda-12gb", "cuda-32gb", "R"]}}
    assert effective_vram_request_gb_from_job(job) == 32.0


def test_effective_vram_implicit_floor_when_only_gpu_true():
    """gpu=True but no explicit vram + no tier tag → 2 GB floor."""
    job = {"requires": {"gpu": True, "needs": []}}
    assert effective_vram_request_gb_from_job(job) == 2.0


def test_effective_vram_zero_for_non_gpu_job():
    """No requires.gpu, no tier tags, no explicit → 0 (gate is a no-op)."""
    job = {"requires": {"gpu": False, "needs": []}}
    assert effective_vram_request_gb_from_job(job) == 0.0


def test_effective_vram_handles_missing_requires():
    """Old job dicts without `requires` key shouldn't crash."""
    assert effective_vram_request_gb_from_job({}) == 0.0


def test_effective_vram_malformed_tier_tag_ignored():
    """`cuda-bigboygb` falls through ValueError; valid tag still wins."""
    job = {"requires": {"gpu": True, "needs": ["cuda-bigboygb", "cuda-12gb"]}}
    assert effective_vram_request_gb_from_job(job) == 12.0


def test_concurrent_ok_marker_detected_in_bash_c():
    """`bash -c "python train.py # CONCURRENT_OK"` form."""
    cmd = ["bash", "-c", "python train.py # CONCURRENT_OK"]
    assert command_has_concurrent_ok(cmd) is True


def test_concurrent_ok_marker_detected_with_no_space():
    cmd = ["bash", "-c", "python train.py #CONCURRENT_OK"]
    assert command_has_concurrent_ok(cmd) is True


def test_concurrent_ok_marker_absent():
    cmd = ["bash", "-c", "python train.py"]
    assert command_has_concurrent_ok(cmd) is False


def test_concurrent_ok_marker_substring_does_not_false_match():
    """Word-boundary anchor: `CONCURRENT_OKAY` mustn't trip the bypass."""
    cmd = ["bash", "-c", "echo CONCURRENT_OKAY"]
    assert command_has_concurrent_ok(cmd) is False


def test_concurrent_ok_marker_handles_empty_cmd():
    assert command_has_concurrent_ok(None) is False
    assert command_has_concurrent_ok([]) is False


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

    Bypass the systemd-run wrap: the wrapper runs the workload in a
    user-scope cgroup, and signal delivery via `systemctl --user kill`
    is environment-dependent (CI containers often lack a user bus). Here
    we care about the signal chain inside run_job, not the scope
    integration — that's covered by tests/integration/test_cancel_latency.py.
    """
    import jobd.worker.job_worker as job_worker

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


def test_run_job_fast_path_skips_systemd_run_wrap(tmp_path, monkeypatch):
    """fast_path=true must bypass the systemd-run scope wrap even when
    systemd-run exists on the worker. Regression guard: a fast-path job
    wrapped in systemd-run --scope pays a ~300ms startup tax per invocation
    and defeats the whole point."""
    import subprocess

    import jobd.worker.job_worker as job_worker

    monkeypatch.setattr(
        job_worker.shutil,
        "which",
        lambda name: f"/fake/bin/{name}" if name == "systemd-run" else None,
    )
    captured: dict[str, list[str]] = {}

    orig_popen = subprocess.Popen

    class _StubProc:
        def __init__(self, cmd, **kw):
            captured["cmd"] = cmd
            self.pid = 12345
            self.stdout = type("F", (), {"read": lambda _s, _n: b"", "close": lambda _s: None})()

        def send_signal(self, _sig):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", _StubProc)

    job = {"id": 9100, "cmd": ["echo", "hi"], "cwd": str(tmp_path), "fast_path": True}
    run_job(_FakeClient(cancel_after_s=999.0), job, set())

    assert captured["cmd"] == ["echo", "hi"], (
        f"fast_path should skip systemd-run wrap, got {captured['cmd']}"
    )
    # sanity: suppress unused warning
    _ = orig_popen


def test_run_job_non_fast_path_uses_named_scope(tmp_path, monkeypatch):
    """Non-fast-path jobs must be wrapped in `systemd-run --scope` with
    a deterministic --unit name keyed off job id and explicit memory caps.
    The scope name is what the cancel path signals via `systemctl --user
    kill`, so it must match the worker's _signal_workload contract.
    Regression for cancel-latency bug (2026-04-26): SIGTERM-to-pid was a
    no-op because pid pointed at the systemd-run client, not at bash."""
    import subprocess

    import jobd.worker.job_worker as job_worker

    monkeypatch.setattr(
        job_worker.shutil,
        "which",
        lambda name: f"/fake/bin/{name}" if name == "systemd-run" else None,
    )
    captured: dict[str, list[str]] = {}

    class _StubProc:
        def __init__(self, cmd, **kw):
            captured["cmd"] = cmd
            self.pid = 12346
            self.stdout = type("F", (), {"read": lambda _s, _n: b"", "close": lambda _s: None})()

        def send_signal(self, _sig):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", _StubProc)
    job = {"id": 9101, "cmd": ["python", "train.py"], "cwd": str(tmp_path), "fast_path": False}
    run_job(_FakeClient(cancel_after_s=999.0), job, set())
    assert captured["cmd"] == [
        "/fake/bin/systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--same-dir",
        "--unit=jobd-9101.scope",
        "-p",
        "MemoryMax=14G",
        "-p",
        "MemorySwapMax=4G",
        "--",
        "python",
        "train.py",
    ], f"unexpected wrap shape: {captured['cmd']}"


def test_run_job_no_systemd_run_runs_bare(tmp_path, monkeypatch):
    """When systemd-run is unavailable (CI containers, minimal images),
    the worker must run the workload directly — no wrap, no crash."""
    import subprocess

    import jobd.worker.job_worker as job_worker

    monkeypatch.setattr(job_worker.shutil, "which", lambda _name: None)
    captured: dict[str, list[str]] = {}

    class _StubProc:
        def __init__(self, cmd, **kw):
            captured["cmd"] = cmd
            self.pid = 12347
            self.stdout = type("F", (), {"read": lambda _s, _n: b"", "close": lambda _s: None})()

        def send_signal(self, _sig):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", _StubProc)
    job = {"id": 9102, "cmd": ["echo", "x"], "cwd": str(tmp_path), "fast_path": False}
    run_job(_FakeClient(cancel_after_s=999.0), job, set())
    assert captured["cmd"] == ["echo", "x"], f"expected bare cmd, got {captured['cmd']}"


def test_run_job_cancel_signals_scope_unit_not_pid(tmp_path, monkeypatch):
    """The cancel signal path must `systemctl --user kill --signal=TERM
    jobd-{id}.scope` — NOT `proc.send_signal`. proc.pid points at the
    systemd-run client, which exits as soon as the scope is set up;
    signaling it is a silent no-op (root cause of the 2026-04-26 cancel-
    latency bug). This test verifies the signaling channel."""
    import subprocess

    import jobd.worker.job_worker as job_worker

    monkeypatch.setattr(
        job_worker.shutil,
        "which",
        lambda name: f"/fake/bin/{name}" if name == "systemd-run" else None,
    )
    systemctl_calls: list[list[str]] = []
    proc_signals: list[int] = []
    cancel_seen = threading.Event()

    class _StubStdout:
        def __init__(self):
            self._first = True

        def read(self, _n):
            # Block long enough for the signal-poll thread to observe cancel
            # and call _signal_workload. Without this, the main loop ends
            # before the poll thread gets scheduled.
            if self._first:
                self._first = False
                cancel_seen.wait(timeout=3.0)
            return b""

        def close(self):
            pass

    class _StubProc:
        def __init__(self, _cmd, **_kw):
            self.pid = 22222
            self.stdout = _StubStdout()

        def send_signal(self, sig):
            proc_signals.append(sig)

        def wait(self, timeout=None):
            return -15

        def kill(self):
            pass

    orig_run = subprocess.run

    def _fake_run(args, **kw):
        if args and args[0] == "systemctl":
            systemctl_calls.append(args)
            cancel_seen.set()

            class R:
                returncode = 0

            return R()
        return orig_run(args, **kw)

    monkeypatch.setattr(subprocess, "Popen", _StubProc)
    monkeypatch.setattr(subprocess, "run", _fake_run)

    job = {"id": 9201, "cmd": ["bash", "-c", "sleep 10"], "cwd": str(tmp_path)}
    client = _FakeClient(cancel_after_s=0.0)
    run_job(client, job, set())

    assert any(
        c[0] == "systemctl" and "--user" in c and "kill" in c and c[-1] == "jobd-9201.scope"
        for c in systemctl_calls
    ), f"expected systemctl --user kill on scope, got {systemctl_calls}"
    assert proc_signals == [], (
        f"cancel must NOT fall through to proc.send_signal when scope is set, got {proc_signals}"
    )
    completes = [p for p in client.posts if p[0].endswith("/complete")]
    assert completes and completes[0][1]["final_state"] == "cancelled"


def test_run_job_posts_started_after_popen(tmp_path, monkeypatch):
    """Right after subprocess.Popen returns, the worker must POST /started so
    the broker can flip assigned -> running. Without this, jobs stay in
    ASSIGNED for their entire run and MCP cancel signal_sent reads as null."""
    import subprocess

    import jobd.worker.job_worker as job_worker

    monkeypatch.setattr(job_worker.shutil, "which", lambda _name: None)

    class _StubProc:
        def __init__(self, _cmd, **_kw):
            self.pid = 12347
            self.stdout = type("F", (), {"read": lambda _s, _n: b"", "close": lambda _s: None})()

        def send_signal(self, _sig):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", _StubProc)
    client = _FakeClient(cancel_after_s=999.0)
    job = {"id": 9200, "cmd": ["echo", "hi"], "cwd": str(tmp_path)}
    run_job(client, job, set())

    started_posts = [p for p in client.posts if p[0] == "/jobs/9200/started"]
    assert len(started_posts) == 1, (
        f"expected exactly one /started POST, got {[p[0] for p in client.posts]}"
    )


def _quiet_stub_proc_pair(rc_on_term: int = -15):
    """Build (StubProc, StubStdout) where stdout blocks until release_event
    is set, then returns b"" (EOF). Lets the test choose when the workload
    "finishes" — used to exercise watchdog-driven kills.
    """
    import subprocess as _subprocess  # noqa: F401

    release_event = threading.Event()

    class _StubStdout:
        def read(self, _n):
            release_event.wait(timeout=10.0)
            return b""

        def close(self):
            pass

    class _StubProc:
        def __init__(self, _cmd, **_kw):
            self.pid = 33333
            self.stdout = _StubStdout()

        def send_signal(self, _sig):
            release_event.set()

        def wait(self, timeout=None):
            release_event.wait(timeout=10.0)
            return rc_on_term

        def kill(self):
            release_event.set()

    return _StubProc, release_event


def test_run_job_idle_timeout_kills_silent_workload(tmp_path, monkeypatch):
    """A job that produces zero output for idle_timeout_s seconds must be
    killed via the scope cancel path and reported as failed with
    termination_reason='idle_timeout'. This is the 2026-04-25 hung-project-c
    failure mode: liveness != progress."""
    import subprocess

    import jobd.worker.job_worker as job_worker

    monkeypatch.setattr(job_worker, "SIGNAL_POLL_INTERVAL_S", 0.05)
    monkeypatch.setattr(
        job_worker.shutil,
        "which",
        lambda name: f"/fake/bin/{name}" if name == "systemd-run" else None,
    )
    StubProc, release = _quiet_stub_proc_pair()
    monkeypatch.setattr(subprocess, "Popen", StubProc)
    systemctl_calls: list[list[str]] = []

    def _fake_run(args, **_kw):
        if args and args[0] == "systemctl":
            systemctl_calls.append(args)
            release.set()

            class R:
                returncode = 0

            return R()

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    job = {
        "id": 9301,
        "cmd": ["bash", "-c", "sleep 60"],
        "cwd": str(tmp_path),
        "idle_timeout_s": 1,
    }
    client = _FakeClient(cancel_after_s=999.0)  # never user-cancel
    t0 = time.monotonic()
    run_job(client, job, set())
    elapsed = time.monotonic() - t0

    assert elapsed < 4.0, f"idle watchdog should fire promptly, took {elapsed:.1f}s"
    assert any("kill" in c and c[-1] == "jobd-9301.scope" for c in systemctl_calls), (
        f"expected systemctl kill on scope unit, got {systemctl_calls}"
    )
    completes = [p for p in client.posts if p[0].endswith("/complete")]
    assert len(completes) == 1
    body = completes[0][1]
    assert body["final_state"] == "failed", (
        f"watchdog timeout must surface as 'failed' so depends_on cascade fails children, got {body}"
    )
    assert body["termination_reason"] == "idle_timeout"


def test_run_job_max_wall_kills_long_running_workload(tmp_path, monkeypatch):
    """A job whose total runtime exceeds max_wall_s must be killed and
    reported as failed with termination_reason='wall_timeout' — even if
    it's producing output (i.e. not idle)."""
    import subprocess

    import jobd.worker.job_worker as job_worker

    monkeypatch.setattr(job_worker, "SIGNAL_POLL_INTERVAL_S", 0.05)
    monkeypatch.setattr(
        job_worker.shutil,
        "which",
        lambda name: f"/fake/bin/{name}" if name == "systemd-run" else None,
    )
    StubProc, release = _quiet_stub_proc_pair()
    monkeypatch.setattr(subprocess, "Popen", StubProc)
    systemctl_calls: list[list[str]] = []

    def _fake_run(args, **_kw):
        if args and args[0] == "systemctl":
            systemctl_calls.append(args)
            release.set()

            class R:
                returncode = 0

            return R()

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    job = {
        "id": 9302,
        "cmd": ["bash", "-c", "yes"],
        "cwd": str(tmp_path),
        "max_wall_s": 1,
    }
    client = _FakeClient(cancel_after_s=999.0)
    t0 = time.monotonic()
    run_job(client, job, set())
    elapsed = time.monotonic() - t0

    assert elapsed < 4.0, f"wall watchdog should fire promptly, took {elapsed:.1f}s"
    completes = [p for p in client.posts if p[0].endswith("/complete")]
    assert len(completes) == 1
    body = completes[0][1]
    assert body["final_state"] == "failed"
    assert body["termination_reason"] == "wall_timeout"


def test_run_job_env_default_idle_timeout(tmp_path, monkeypatch):
    """JOBD_WORKER_IDLE_TIMEOUT_S env var supplies a default when the job
    doesn't specify idle_timeout_s. Lets ops set a fleet-wide watchdog
    without changing every call site."""
    import subprocess

    import jobd.worker.job_worker as job_worker

    monkeypatch.setenv("JOBD_WORKER_IDLE_TIMEOUT_S", "1")
    monkeypatch.setattr(job_worker, "SIGNAL_POLL_INTERVAL_S", 0.05)
    monkeypatch.setattr(job_worker.shutil, "which", lambda _name: None)
    StubProc, release = _quiet_stub_proc_pair()
    monkeypatch.setattr(subprocess, "Popen", StubProc)

    def _fake_run(args, **_kw):
        release.set()

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    job = {"id": 9303, "cmd": ["bash", "-c", "sleep 60"], "cwd": str(tmp_path)}
    client = _FakeClient(cancel_after_s=999.0)
    t0 = time.monotonic()
    run_job(client, job, set())
    elapsed = time.monotonic() - t0
    assert elapsed < 4.0, f"env-default idle watchdog should fire promptly, took {elapsed:.1f}s"
    completes = [p for p in client.posts if p[0].endswith("/complete")]
    assert len(completes) == 1
    assert completes[0][1]["termination_reason"] == "idle_timeout"


def test_user_cancel_surfaces_as_cancelled_not_failed(tmp_path, monkeypatch):
    """Regression guard: the watchdog logic flips final_state from
    'cancelled' to 'failed' only when termination_reason is set. A
    user-initiated /cancel must still land as 'cancelled' so the manual-
    abort surface stays clean."""
    import subprocess

    import jobd.worker.job_worker as job_worker

    monkeypatch.setattr(job_worker, "SIGNAL_POLL_INTERVAL_S", 0.05)
    monkeypatch.setattr(job_worker.shutil, "which", lambda _name: None)
    StubProc, release = _quiet_stub_proc_pair()
    monkeypatch.setattr(subprocess, "Popen", StubProc)

    def _fake_run(_args, **_kw):
        release.set()

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    job = {"id": 9304, "cmd": ["bash", "-c", "sleep 5"], "cwd": str(tmp_path)}
    client = _FakeClient(cancel_after_s=0.05)  # cancel almost immediately
    run_job(client, job, set())

    completes = [p for p in client.posts if p[0].endswith("/complete")]
    assert len(completes) == 1
    body = completes[0][1]
    assert body["final_state"] == "cancelled", body
    # No termination_reason should be set for user cancels
    assert "termination_reason" not in body or body.get("termination_reason") is None


def test_run_job_does_not_post_started_when_popen_fails(tmp_path, monkeypatch):
    """If Popen raises (bad cmd, missing executable), the worker reports
    /complete with exit 127 — and must NOT POST /started, because there's
    no live subprocess to be 'running'."""
    import subprocess

    import jobd.worker.job_worker as job_worker

    monkeypatch.setattr(job_worker.shutil, "which", lambda _name: None)

    def _boom(*_a, **_kw):
        raise FileNotFoundError("nope")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    client = _FakeClient(cancel_after_s=999.0)
    job = {"id": 9201, "cmd": ["does-not-exist"], "cwd": str(tmp_path)}
    run_job(client, job, set())

    started_posts = [p for p in client.posts if p[0].endswith("/started")]
    assert started_posts == [], f"unexpected /started POST after Popen failure: {client.posts}"
    complete_posts = [p for p in client.posts if p[0].endswith("/complete")]
    assert len(complete_posts) == 1
    assert complete_posts[0][1]["exit_code"] == 127


class _PreemptClient:
    """FakeClient variant: returns signal=preempt after preempt_after_s.

    Records all POSTs (raw bytes for /log, parsed JSON for everything
    else) so tests can verify both the side effects of preempt handling
    and the bodies of /complete and /checkpoint-complete.
    """

    def __init__(self, preempt_after_s: float):
        self.preempt_after_s = preempt_after_s
        self.t0 = time.monotonic()
        self.posts: list[tuple[str, object]] = []
        self.lock = threading.Lock()

    def get(self, path, **kw):
        if path.endswith("/signal"):
            elapsed = time.monotonic() - self.t0
            if elapsed >= self.preempt_after_s:
                return _FakeResp(200, {"signal": "preempt"})
            return _FakeResp(200, {"signal": None})
        return _FakeResp(200, {})

    def post(self, path, json=None, content=None, **kw):
        with self.lock:
            payload: object = json if json is not None else (content or b"")
            self.posts.append((path, payload))
        return _FakeResp(200, {})


def test_run_job_preempt_with_checkpoint_token_emits_checkpoint_complete(tmp_path, monkeypatch):
    """#37: when the workload prints `jobd-checkpoint-complete` after
    SIGTERM during a preempt, the worker POSTs /checkpoint-complete and
    reports final_state=preempted."""
    import jobd.worker.job_worker as job_worker

    monkeypatch.setattr(job_worker.shutil, "which", lambda _name: None)

    job = {
        "id": 9301,
        "cmd": [
            "python3",
            "-c",
            "import signal,sys,time\n"
            "signal.signal(signal.SIGTERM, lambda *a: ("
            "print('jobd-checkpoint-complete', flush=True), sys.exit(0)))\n"
            "time.sleep(30)\n",
        ],
        "cwd": str(tmp_path),
        "checkpoint_grace_s": 30,
    }
    client = _PreemptClient(preempt_after_s=1.0)
    run_job(client, job, set())

    paths = [p for p, _ in client.posts]
    assert any(p.endswith("/checkpoint-complete") for p in paths), (
        f"expected /checkpoint-complete POST, got {paths}"
    )
    completes = [
        p
        for p in client.posts
        if p[0].endswith("/complete") and not p[0].endswith("/checkpoint-complete")
    ]
    assert len(completes) == 1
    assert completes[0][1]["final_state"] == "preempted"


def test_run_job_preempt_without_token_skips_checkpoint_complete(tmp_path, monkeypatch):
    """#37: a preempted workload that exits without printing the token
    still completes as preempted, but no /checkpoint-complete POST fires."""
    import jobd.worker.job_worker as job_worker

    monkeypatch.setattr(job_worker.shutil, "which", lambda _name: None)

    job = {
        "id": 9302,
        "cmd": ["bash", "-c", "sleep 30"],
        "cwd": str(tmp_path),
    }
    client = _PreemptClient(preempt_after_s=1.0)
    run_job(client, job, set())

    paths = [p for p, _ in client.posts]
    assert not any(p.endswith("/checkpoint-complete") for p in paths), (
        f"unexpected /checkpoint-complete POST: {paths}"
    )
    completes = [p for p in client.posts if p[0].endswith("/complete")]
    assert len(completes) == 1
    assert completes[0][1]["final_state"] == "preempted"


def test_run_job_preempt_honors_checkpoint_grace_s(tmp_path, monkeypatch):
    """#37: an explicit checkpoint_grace_s overrides the worker's 60s
    default — a workload that ignores SIGTERM gets SIGKILLed after the
    configured grace window, not after a minute."""
    import jobd.worker.job_worker as job_worker

    monkeypatch.setattr(job_worker.shutil, "which", lambda _name: None)

    job = {
        "id": 9303,
        "cmd": [
            "python3",
            "-c",
            "import signal,time\nsignal.signal(signal.SIGTERM, lambda *a: None)\ntime.sleep(30)\n",
        ],
        "cwd": str(tmp_path),
        "checkpoint_grace_s": 2,
    }
    client = _PreemptClient(preempt_after_s=1.0)

    t0 = time.monotonic()
    run_job(client, job, set())
    elapsed = time.monotonic() - t0

    assert elapsed < 6.0, f"expected ~3s with grace=2, got {elapsed:.1f}s"
    completes = [p for p in client.posts if p[0].endswith("/complete")]
    assert completes[0][1]["final_state"] == "preempted"


def test_run_job_exports_checkpoint_grace_env_for_workload(tmp_path, monkeypatch):
    """#38: worker exposes the resolved per-job checkpoint_grace_s to the
    workload via JOBD_CHECKPOINT_GRACE_S so install_preemption_handler
    can compute time_remaining()."""
    import jobd.worker.job_worker as job_worker

    monkeypatch.setattr(job_worker.shutil, "which", lambda _name: None)

    out_path = tmp_path / "env.txt"
    job = {
        "id": 9501,
        "cmd": [
            "bash",
            "-c",
            f"echo $JOBD_CHECKPOINT_GRACE_S > {out_path}",
        ],
        "cwd": str(tmp_path),
        "checkpoint_grace_s": 90,
    }
    run_job(_FakeClient(cancel_after_s=999.0), job, set())
    assert out_path.read_text().strip() == "90"


def test_run_job_exports_default_checkpoint_grace_env_when_unset(tmp_path, monkeypatch):
    """No checkpoint_grace_s on the job → worker exports the default 60s
    so install_preemption_handler always has a sane value to read."""
    import jobd.worker.job_worker as job_worker

    monkeypatch.setattr(job_worker.shutil, "which", lambda _name: None)

    out_path = tmp_path / "env.txt"
    job = {
        "id": 9502,
        "cmd": [
            "bash",
            "-c",
            f"echo $JOBD_CHECKPOINT_GRACE_S > {out_path}",
        ],
        "cwd": str(tmp_path),
    }
    run_job(_FakeClient(cancel_after_s=999.0), job, set())
    assert out_path.read_text().strip() == "60"


# ---- #42 multi-tenant per-host co-routing -------------------------------


def _reset_in_flight():
    with job_worker._in_flight_lock:
        job_worker._in_flight.clear()


def test_max_concurrent_jobs_default_is_1(monkeypatch):
    """Default preserves single-slot behavior — opt-in via env."""
    monkeypatch.delenv("JOBD_WORKER_MAX_CONCURRENT_JOBS", raising=False)
    assert job_worker._max_concurrent_jobs() == 1


def test_max_concurrent_jobs_reads_env(monkeypatch):
    monkeypatch.setenv("JOBD_WORKER_MAX_CONCURRENT_JOBS", "4")
    assert job_worker._max_concurrent_jobs() == 4


def test_max_concurrent_jobs_floor_is_1(monkeypatch):
    """Garbage / negative env values fall back to 1, never 0."""
    monkeypatch.setenv("JOBD_WORKER_MAX_CONCURRENT_JOBS", "0")
    assert job_worker._max_concurrent_jobs() == 1
    monkeypatch.setenv("JOBD_WORKER_MAX_CONCURRENT_JOBS", "-3")
    assert job_worker._max_concurrent_jobs() == 1
    monkeypatch.setenv("JOBD_WORKER_MAX_CONCURRENT_JOBS", "not-a-number")
    assert job_worker._max_concurrent_jobs() == 1


def test_register_in_flight_records_ad():
    _reset_in_flight()
    try:
        job_worker._register_in_flight({"id": 901, "vram_gb": 8.0, "ram_gb": 4.0, "cpus": 2})
        v, r, c = job_worker._allocations_total()
        assert v == 8.0
        assert r == 4.0
        assert c == 2
    finally:
        _reset_in_flight()


def test_register_in_flight_uses_effective_vram_for_tier_tag():
    """vram_gb=0 + cuda-32gb in needs → tier ladder fallback in the ad."""
    _reset_in_flight()
    try:
        job_worker._register_in_flight(
            {
                "id": 902,
                "vram_gb": 0,
                "requires": {"needs": ["cuda-32gb"]},
                "ram_gb": 0,
                "cpus": 0,
            }
        )
        v, _, _ = job_worker._allocations_total()
        assert v == 32.0
    finally:
        _reset_in_flight()


def test_unregister_in_flight_clears_ad():
    _reset_in_flight()
    try:
        job_worker._register_in_flight({"id": 903, "vram_gb": 8.0, "ram_gb": 4.0, "cpus": 2})
        job_worker._unregister_in_flight(903)
        assert job_worker._allocations_total() == (0.0, 0.0, 0)
    finally:
        _reset_in_flight()


def test_allocations_sum_across_multiple_in_flight():
    _reset_in_flight()
    try:
        job_worker._register_in_flight({"id": 910, "vram_gb": 8.0, "ram_gb": 4.0, "cpus": 2})
        job_worker._register_in_flight({"id": 911, "vram_gb": 12.0, "ram_gb": 6.0, "cpus": 3})
        v, r, c = job_worker._allocations_total()
        assert v == 20.0
        assert r == 10.0
        assert c == 5
    finally:
        _reset_in_flight()


def test_resource_snapshot_subtracts_in_flight_vram(monkeypatch):
    """Heartbeat must report adjusted (raw - in-flight) free, so the broker
    matcher only sees true-available capacity. Without this, a worker
    running one 24 GB job still advertises its full GPU as free."""
    _reset_in_flight()
    try:
        monkeypatch.setattr(job_worker, "nvidia_free_vram_gb", lambda: 32.0)
        monkeypatch.setattr(job_worker, "nvidia_processes", lambda: [])
        job_worker._register_in_flight({"id": 920, "vram_gb": 24.0, "ram_gb": 0, "cpus": 0})
        snap = job_worker.resource_snapshot(set())
        assert snap["free_vram_gb"] == 8.0
    finally:
        _reset_in_flight()


def test_resource_snapshot_floor_at_zero_when_overcommitted(monkeypatch):
    """In-flight ad larger than current raw free (e.g., the workload
    crashed and freed VRAM but the ad still reflects the matcher's
    reservation) — clamp at 0 rather than going negative."""
    _reset_in_flight()
    try:
        monkeypatch.setattr(job_worker, "nvidia_free_vram_gb", lambda: 4.0)
        monkeypatch.setattr(job_worker, "nvidia_processes", lambda: [])
        job_worker._register_in_flight({"id": 930, "vram_gb": 24.0, "ram_gb": 0, "cpus": 0})
        snap = job_worker.resource_snapshot(set())
        assert snap["free_vram_gb"] == 0.0
    finally:
        _reset_in_flight()


def test_resource_snapshot_no_in_flight_unchanged(monkeypatch):
    """No allocations → snapshot reports raw NVML readings unchanged."""
    _reset_in_flight()
    monkeypatch.setattr(job_worker, "nvidia_free_vram_gb", lambda: 30.0)
    monkeypatch.setattr(job_worker, "nvidia_processes", lambda: [])
    snap = job_worker.resource_snapshot(set())
    assert snap["free_vram_gb"] == 30.0


def test_resource_snapshot_reports_slot_usage(monkeypatch):
    """Heartbeat carries max_concurrent (the env knob) + running (live
    in-flight count) so the broker can surface slot usage in `job workers`."""
    _reset_in_flight()
    try:
        monkeypatch.setattr(job_worker, "nvidia_free_vram_gb", lambda: 30.0)
        monkeypatch.setattr(job_worker, "nvidia_processes", lambda: [])
        monkeypatch.setenv("JOBD_WORKER_MAX_CONCURRENT_JOBS", "3")
        job_worker._register_in_flight({"id": 1, "vram_gb": 0, "ram_gb": 0, "cpus": 1})
        job_worker._register_in_flight({"id": 2, "vram_gb": 0, "ram_gb": 0, "cpus": 1})
        snap = job_worker.resource_snapshot(set())
        assert snap["max_concurrent"] == 3
        assert snap["running"] == 2
    finally:
        _reset_in_flight()


def test_resource_snapshot_reports_in_flight_job_ids(monkeypatch):
    """SIGTERM-drain Phase 2: every heartbeat carries the worker's in-flight
    job ids so the broker can reconcile claims a restarted worker no longer
    knows about (docs/plans/sigterm-drain.md). Sorted for determinism."""
    _reset_in_flight()
    try:
        monkeypatch.setattr(job_worker, "nvidia_free_vram_gb", lambda: 30.0)
        monkeypatch.setattr(job_worker, "nvidia_processes", lambda: [])
        snap = job_worker.resource_snapshot(set())
        assert snap["in_flight_job_ids"] == []
        job_worker._register_in_flight({"id": 972, "vram_gb": 0, "ram_gb": 0, "cpus": 1})
        job_worker._register_in_flight({"id": 971, "vram_gb": 0, "ram_gb": 0, "cpus": 1})
        snap = job_worker.resource_snapshot(set())
        assert snap["in_flight_job_ids"] == [971, 972]
    finally:
        _reset_in_flight()


def test_effective_owned_pids_unions_scope_cgroup_pids(tmp_path, monkeypatch):
    """A scope-wrapped job's real CUDA pids live in its cgroup, not in
    tracked_pids (which holds the systemd-run client pid). The owned set must
    union both — read from a real cgroup.procs file."""
    _reset_in_flight()
    try:
        scope_dir = tmp_path / "jobd-9300.scope"
        scope_dir.mkdir()
        (scope_dir / "cgroup.procs").write_text("4242\n4243\n")
        monkeypatch.setattr(job_worker, "_REAPER_OK", True)
        monkeypatch.setattr(
            job_worker._cgroup_walk,
            "resolve_user_scope_path",
            lambda unit: scope_dir if unit == "jobd-9300.scope" else None,
        )
        job_worker._register_in_flight({"id": 9300, "vram_gb": 8, "ram_gb": 0, "cpus": 0})
        # 999 = the systemd-run client pid we Popen'd; 4242/4243 = real workload.
        assert job_worker._effective_owned_pids({999}) == {999, 4242, 4243}
    finally:
        _reset_in_flight()


def test_effective_owned_pids_no_scope_is_just_tracked(monkeypatch):
    """Fast-path / unwrapped jobs have no scope cgroup (resolve → None); the
    owned set is then exactly tracked_pids (the real child is already there)."""
    _reset_in_flight()
    try:
        monkeypatch.setattr(job_worker, "_REAPER_OK", True)
        monkeypatch.setattr(job_worker._cgroup_walk, "resolve_user_scope_path", lambda unit: None)
        job_worker._register_in_flight({"id": 9301, "vram_gb": 0, "ram_gb": 0, "cpus": 0})
        assert job_worker._effective_owned_pids({777}) == {777}
    finally:
        _reset_in_flight()


def test_own_scope_job_not_counted_as_foreign_vram(tmp_path, monkeypatch):
    """The P3.3 bug: NVML reports a forked-child pid (inside the scope cgroup)
    holding the VRAM, but tracked_pids only has the job's top-level pid — so the
    worker's OWN child was mis-counted as foreign/unregistered. With the
    effective-owned-pids union (cgroup is the ownership boundary) it reads as 0
    unregistered. RTX 5090 real-exec confirmed NVML reports the child pid."""
    _reset_in_flight()
    try:
        scope_dir = tmp_path / "jobd-9302.scope"
        scope_dir.mkdir()
        (scope_dir / "cgroup.procs").write_text("5000\n")  # the real CUDA pid
        monkeypatch.setattr(job_worker, "_REAPER_OK", True)
        monkeypatch.setattr(
            job_worker._cgroup_walk,
            "resolve_user_scope_path",
            lambda unit: scope_dir if unit == "jobd-9302.scope" else None,
        )
        monkeypatch.setattr(job_worker, "nvidia_processes", lambda: [(5000, 8192)])
        monkeypatch.setattr(job_worker, "nvidia_free_vram_gb", lambda: 24.0)
        job_worker._register_in_flight({"id": 9302, "vram_gb": 8, "ram_gb": 0, "cpus": 0})
        # tracked_pids holds only the top-level pid 999; the forked CUDA child
        # (5000) is owned via the cgroup, so it is NOT foreign.
        snap = job_worker.resource_snapshot({999})
        assert snap["unregistered_vram_gb"] == 0.0
    finally:
        _reset_in_flight()


def test_is_solo_in_flight_gates_reparented_orphan_sweep():
    """P3.5: the reparented-orphan /proc sweep must only run when this is the
    sole in-flight job. With a concurrent job registered, the global sweep
    could kill the other job's reparented fast-path descendant, so the gate
    returns False and run_job skips it."""
    _reset_in_flight()
    try:
        assert job_worker._is_solo_in_flight() is True  # nothing in flight
        job_worker._register_in_flight({"id": 960, "vram_gb": 0, "ram_gb": 0, "cpus": 0})
        assert job_worker._is_solo_in_flight() is True  # just me
        job_worker._register_in_flight({"id": 961, "vram_gb": 0, "ram_gb": 0, "cpus": 0})
        assert job_worker._is_solo_in_flight() is False  # a concurrent job exists
        job_worker._unregister_in_flight(961)
        assert job_worker._is_solo_in_flight() is True  # back to solo
    finally:
        _reset_in_flight()


def test_reserve_and_dispatch_registers_before_returning_threaded():
    """Over-subscribe race regression (P3.4): the in-flight reservation must be
    taken SYNCHRONOUSLY — visible the instant _reserve_and_dispatch returns,
    before the worker thread has even run. If registration were deferred into
    the thread (the pre-fix behavior), the poll loop's capacity gate could
    re-poll against a stale count and oversubscribe past max_concurrent."""
    _reset_in_flight()
    started = threading.Event()
    release = threading.Event()

    def fake_run(job):
        started.set()
        release.wait(5)  # keep the job "in flight" so we can observe it
        job_worker._unregister_in_flight(int(job["id"]))

    threads: list = []
    try:
        job_worker._reserve_and_dispatch(
            {"id": 940, "vram_gb": 8.0, "ram_gb": 4.0, "cpus": 2},
            max_concurrent=4,
            job_threads=threads,
            run_in_thread=fake_run,
        )
        # Reservation visible immediately — the gate would see it next poll.
        with job_worker._in_flight_lock:
            assert 940 in job_worker._in_flight
        assert len(threads) == 1
        assert started.wait(5), "worker thread never started"
        release.set()
        threads[0].join(5)
        # Thread's finally unregisters once the job finishes.
        with job_worker._in_flight_lock:
            assert 940 not in job_worker._in_flight
    finally:
        release.set()
        _reset_in_flight()


def test_reserve_and_dispatch_single_slot_also_threads():
    """SIGTERM-drain prerequisite: even at max_concurrent == 1 the job must run
    in a worker thread, never inline in the poll loop. Inline execution parks
    the main thread inside proc.stdout.read() for the whole job, so a drain
    can't start until the job ends naturally (docs/plans/sigterm-drain.md)."""
    _reset_in_flight()
    started = threading.Event()
    release = threading.Event()
    ran: list = []

    def fake_run(job):
        started.set()
        release.wait(5)  # if this ran inline, _reserve_and_dispatch would hang
        ran.append(int(job["id"]))
        job_worker._unregister_in_flight(int(job["id"]))

    threads: list = []
    try:
        job_worker._reserve_and_dispatch(
            {"id": 941, "vram_gb": 8.0, "ram_gb": 4.0, "cpus": 2},
            max_concurrent=1,
            job_threads=threads,
            run_in_thread=fake_run,
        )
        # Returned while the job is still running — therefore not inline.
        assert len(threads) == 1
        with job_worker._in_flight_lock:
            assert 941 in job_worker._in_flight  # reservation already visible
        assert started.wait(5), "worker thread never started"
        release.set()
        threads[0].join(5)
        assert ran == [941]
        with job_worker._in_flight_lock:
            assert 941 not in job_worker._in_flight
    finally:
        release.set()
        _reset_in_flight()


def test_reserve_and_dispatch_caps_concurrent_reservations():
    """Driving N claims through _reserve_and_dispatch with a gate check between
    each (mirroring the poll loop) never exceeds max_concurrent live
    reservations — the synchronous registration closes the race window."""
    _reset_in_flight()
    release = threading.Event()
    max_concurrent = 3
    threads: list = []

    def fake_run(job):
        release.wait(5)
        job_worker._unregister_in_flight(int(job["id"]))

    try:
        admitted = 0
        for jid in range(950, 960):  # 10 candidate jobs, cap is 3
            with job_worker._in_flight_lock:
                live = len(job_worker._in_flight)
            if live >= max_concurrent:
                continue  # gate refuses, exactly as the poll loop does
            job_worker._reserve_and_dispatch(
                {"id": jid, "vram_gb": 8.0, "ram_gb": 4.0, "cpus": 2},
                max_concurrent=max_concurrent,
                job_threads=threads,
                run_in_thread=fake_run,
            )
            admitted += 1
            with job_worker._in_flight_lock:
                assert len(job_worker._in_flight) <= max_concurrent
        assert admitted == max_concurrent
        release.set()
        for t in threads:
            t.join(5)
    finally:
        release.set()
        _reset_in_flight()


def test_run_job_exposes_checkpoint_dir(tmp_path, monkeypatch):
    """run_job must set JOBD_CHECKPOINT_DIR in the spawned env, pointing at a
    pre-created directory under the default root (~/.local/share/jobd/checkpoints).
    Workloads use this to write durable checkpoints during a preempt."""
    import subprocess

    import jobd.worker.job_worker as job_worker

    monkeypatch.setattr(
        job_worker.shutil,
        "which",
        lambda name: f"/fake/bin/{name}" if name == "systemd-run" else None,
    )
    # Force the default root: clear override, redirect HOME to tmp so we don't
    # touch the real ~/.local/share.
    monkeypatch.delenv("JOBD_WORKER_CHECKPOINT_ROOT", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    captured: dict[str, dict] = {}

    class _StubProc:
        def __init__(self, cmd, **kw):
            captured["env"] = kw.get("env") or {}
            self.pid = 12345
            self.stdout = type("F", (), {"read": lambda _s, _n: b"", "close": lambda _s: None})()

        def send_signal(self, _sig):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", _StubProc)

    job = {
        "id": 9200,
        "cmd": ["echo", "hi"],
        "cwd": str(tmp_path),
        "fast_path": True,
    }
    run_job(_FakeClient(cancel_after_s=999.0), job, set())

    ckpt_dir = captured["env"].get("JOBD_CHECKPOINT_DIR")
    assert ckpt_dir, f"JOBD_CHECKPOINT_DIR not set; env keys: {sorted(captured['env'].keys())}"
    expected_root = tmp_path / ".local" / "share" / "jobd" / "checkpoints"
    assert ckpt_dir == str(expected_root / "9200"), (
        f"expected {expected_root / '9200'}, got {ckpt_dir}"
    )
    assert (expected_root / "9200").is_dir(), f"directory not created at {ckpt_dir}"


def test_run_job_propagates_submitted_env(tmp_path, monkeypatch):
    """P0.3 (CRIT M1): caller-submitted env vars (`job["env"]`) reach the
    workload subprocess. They layer over the worker's inherited environment,
    and jobd-internal JOBD_* vars still take precedence (a caller cannot
    clobber JOBD_CHECKPOINT_DIR)."""
    import subprocess

    import jobd.worker.job_worker as job_worker

    monkeypatch.setattr(
        job_worker.shutil,
        "which",
        lambda name: f"/fake/bin/{name}" if name == "systemd-run" else None,
    )
    monkeypatch.delenv("JOBD_WORKER_CHECKPOINT_ROOT", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    captured: dict[str, dict] = {}

    class _StubProc:
        def __init__(self, cmd, **kw):
            captured["env"] = kw.get("env") or {}
            self.pid = 12346
            self.stdout = type("F", (), {"read": lambda _s, _n: b"", "close": lambda _s: None})()

        def send_signal(self, _sig):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", _StubProc)

    job = {
        "id": 9300,
        "cmd": ["printenv"],
        "cwd": str(tmp_path),
        "fast_path": True,
        "env": {
            "MY_TOKEN": "abc123",
            # A caller MUST NOT be able to clobber jobd-internal vars.
            "JOBD_CHECKPOINT_DIR": "/tmp/evil",
        },
    }
    run_job(_FakeClient(cancel_after_s=999.0), job, set())

    assert captured["env"].get("MY_TOKEN") == "abc123"
    # jobd-internal var wins over a caller attempting to override it.
    assert captured["env"].get("JOBD_CHECKPOINT_DIR") != "/tmp/evil"
    assert captured["env"]["JOBD_CHECKPOINT_DIR"].endswith("/9300")


def test_run_job_checkpoint_dir_root_override(tmp_path, monkeypatch):
    """JOBD_WORKER_CHECKPOINT_ROOT overrides the default root. Used when an
    operator wants checkpoints on a different filesystem (e.g., a fast NVMe
    instead of HDD-backed home)."""
    import subprocess

    import jobd.worker.job_worker as job_worker

    monkeypatch.setattr(
        job_worker.shutil,
        "which",
        lambda name: f"/fake/bin/{name}" if name == "systemd-run" else None,
    )
    override_root = tmp_path / "fast-disk" / "jobd-ckpts"
    monkeypatch.setenv("JOBD_WORKER_CHECKPOINT_ROOT", str(override_root))

    captured: dict[str, dict] = {}

    class _StubProc:
        def __init__(self, cmd, **kw):
            captured["env"] = kw.get("env") or {}
            self.pid = 12346
            self.stdout = type("F", (), {"read": lambda _s, _n: b"", "close": lambda _s: None})()

        def send_signal(self, _sig):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", _StubProc)

    job = {
        "id": 9201,
        "cmd": ["echo", "hi"],
        "cwd": str(tmp_path),
        "fast_path": True,
    }
    run_job(_FakeClient(cancel_after_s=999.0), job, set())

    ckpt_dir = captured["env"]["JOBD_CHECKPOINT_DIR"]
    assert ckpt_dir == str(override_root / "9201"), (
        f"override not honored: expected {override_root / '9201'}, got {ckpt_dir}"
    )
    assert (override_root / "9201").is_dir()


def test_run_job_checkpoint_dir_override_expands_tilde(tmp_path, monkeypatch):
    """JOBD_WORKER_CHECKPOINT_ROOT supports ~-prefixed paths via expanduser,
    matching the default branch. Operators setting the env var via systemd
    Environment= (where shell expansion doesn't happen) should not get a
    literal ~/ckpts directory."""
    import subprocess

    import jobd.worker.job_worker as job_worker

    monkeypatch.setattr(
        job_worker.shutil,
        "which",
        lambda name: f"/fake/bin/{name}" if name == "systemd-run" else None,
    )
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("JOBD_WORKER_CHECKPOINT_ROOT", "~/ckpts-override")

    captured: dict[str, dict] = {}

    class _StubProc:
        def __init__(self, cmd, **kw):
            captured["env"] = kw.get("env") or {}
            self.pid = 12347
            self.stdout = type("F", (), {"read": lambda _s, _n: b"", "close": lambda _s: None})()

        def send_signal(self, _sig):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", _StubProc)

    job = {
        "id": 9202,
        "cmd": ["echo", "hi"],
        "cwd": str(tmp_path),
        "fast_path": True,
    }
    run_job(_FakeClient(cancel_after_s=999.0), job, set())

    ckpt_dir = captured["env"]["JOBD_CHECKPOINT_DIR"]
    expected = fake_home / "ckpts-override" / "9202"
    assert ckpt_dir == str(expected), (
        f"~ not expanded in override: expected {expected}, got {ckpt_dir}"
    )
    # Sanity: literal-~ path should NOT exist
    assert not (tmp_path / "~" / "ckpts-override").exists()
    assert expected.is_dir()


def test_run_job_checkpoint_dir_xdg_data_home_branch(tmp_path, monkeypatch):
    """XDG_DATA_HOME (when set and non-empty) wins over the ~/.local/share
    fallback. Closes the contract→test gap on resolution rule #2."""
    import subprocess

    import jobd.worker.job_worker as job_worker

    monkeypatch.setattr(
        job_worker.shutil,
        "which",
        lambda name: f"/fake/bin/{name}" if name == "systemd-run" else None,
    )
    monkeypatch.delenv("JOBD_WORKER_CHECKPOINT_ROOT", raising=False)
    xdg_root = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_root))
    monkeypatch.setenv("HOME", str(tmp_path / "home-should-not-be-used"))

    captured: dict[str, dict] = {}

    class _StubProc:
        def __init__(self, cmd, **kw):
            captured["env"] = kw.get("env") or {}
            self.pid = 12348
            self.stdout = type("F", (), {"read": lambda _s, _n: b"", "close": lambda _s: None})()

        def send_signal(self, _sig):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", _StubProc)

    job = {
        "id": 9203,
        "cmd": ["echo", "hi"],
        "cwd": str(tmp_path),
        "fast_path": True,
    }
    run_job(_FakeClient(cancel_after_s=999.0), job, set())

    ckpt_dir = captured["env"]["JOBD_CHECKPOINT_DIR"]
    expected = xdg_root / "jobd" / "checkpoints" / "9203"
    assert ckpt_dir == str(expected), (
        f"XDG_DATA_HOME branch not taken: expected {expected}, got {ckpt_dir}"
    )
    assert expected.is_dir()


def test_missing_launcher_path_absolute_missing(tmp_path):
    missing = tmp_path / "nonexistent.sh"
    assert _missing_launcher_path([str(missing), "arg"], cwd=str(tmp_path)) == str(missing)


def test_missing_launcher_path_relative_missing(tmp_path):
    out = _missing_launcher_path(["./nope.sh"], cwd=str(tmp_path))
    assert out == str(tmp_path / "nope.sh")


def test_missing_launcher_path_existing_executable_is_none(tmp_path):
    script = tmp_path / "ok.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    assert _missing_launcher_path([str(script)], cwd=str(tmp_path)) is None


def test_missing_launcher_path_existing_non_executable_caught(tmp_path):
    script = tmp_path / "noexec.sh"
    script.write_text("hi\n")
    script.chmod(0o644)
    assert _missing_launcher_path([str(script)], cwd=str(tmp_path)) == str(script)


def test_missing_launcher_path_path_resolved_name_skipped(tmp_path):
    # `bash`, `python`, `ctest` etc. — Popen resolves these via PATH; we
    # intentionally don't pre-check them. Returns None even though no such
    # absolute file exists.
    assert _missing_launcher_path(["bash", "./inner.sh"], cwd=str(tmp_path)) is None
    assert _missing_launcher_path(["python", "-c", "print(1)"], cwd=str(tmp_path)) is None


def test_missing_launcher_path_empty_cmd():
    assert _missing_launcher_path([], cwd="/tmp") is None


def test_run_job_refuses_dispatch_when_launcher_missing(tmp_path, monkeypatch):
    """Regression: jobs 261/262 (2026-04-30) failed exit 127 because the
    launcher wasn't committed. Worker must surface this at dispatch with
    termination_reason=launcher_missing rather than exec-ing into a silent
    failure inside the systemd-run scope."""
    import subprocess

    monkeypatch.setattr(job_worker.shutil, "which", lambda _name: None)

    popen_calls: list[list[str]] = []

    def _no_popen(cmd, **kw):
        popen_calls.append(cmd)
        raise AssertionError(f"Popen must not be called when launcher is missing; got {cmd}")

    monkeypatch.setattr(subprocess, "Popen", _no_popen)

    missing_path = str(tmp_path / "never_committed.sh")
    job = {"id": 9999, "cmd": [missing_path, "--arg"], "cwd": str(tmp_path)}
    client = _FakeClient(cancel_after_s=999.0)

    run_job(client, job, set())

    assert popen_calls == [], "Popen should not be reached for a missing launcher"
    completes = [p for p in client.posts if p[0].endswith("/complete")]
    assert len(completes) == 1, f"expected one /complete, got posts={client.posts}"
    body = completes[0][1]
    assert body["final_state"] == "failed"
    assert body["exit_code"] == 127
    assert body["termination_reason"] == "launcher_missing"
    # The offending path should also be logged via /log for diagnostics.
    # (_FakeClient stores content-POSTs as {"_raw": byte_length}; we just
    # check that a non-empty log line landed.)
    log_posts = [p for p in client.posts if p[0].endswith("/log")]
    assert len(log_posts) == 1
    assert log_posts[0][1].get("_raw", 0) > 0


def test_run_job_first_output_timeout_kills_silent_start(tmp_path, monkeypatch):
    """A job that produces zero output within
    JOBD_WORKER_FIRST_OUTPUT_TIMEOUT_S of dispatch must be killed via
    the scope cancel path and reported as failed with
    termination_reason='first_output_timeout'. Distinct from idle_timeout
    in that it has a typically-shorter, fires-once-only deadline."""
    import subprocess

    monkeypatch.setenv("JOBD_WORKER_FIRST_OUTPUT_TIMEOUT_S", "1")
    monkeypatch.setattr(job_worker, "SIGNAL_POLL_INTERVAL_S", 0.05)
    monkeypatch.setattr(
        job_worker.shutil,
        "which",
        lambda name: f"/fake/bin/{name}" if name == "systemd-run" else None,
    )
    StubProc, release = _quiet_stub_proc_pair()
    monkeypatch.setattr(subprocess, "Popen", StubProc)
    systemctl_calls: list[list[str]] = []

    def _fake_run(args, **_kw):
        if args and args[0] == "systemctl":
            systemctl_calls.append(args)
            release.set()

            class R:
                returncode = 0

            return R()

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    job = {"id": 9401, "cmd": ["bash", "-c", "sleep 60"], "cwd": str(tmp_path)}
    client = _FakeClient(cancel_after_s=999.0)
    t0 = time.monotonic()
    run_job(client, job, set())
    elapsed = time.monotonic() - t0

    assert elapsed < 4.0, f"first-output watchdog should fire promptly, took {elapsed:.1f}s"
    assert any("kill" in c and c[-1] == "jobd-9401.scope" for c in systemctl_calls), (
        f"expected systemctl kill on scope unit, got {systemctl_calls}"
    )
    completes = [p for p in client.posts if p[0].endswith("/complete")]
    assert len(completes) == 1
    body = completes[0][1]
    assert body["final_state"] == "failed"
    assert body["termination_reason"] == "first_output_timeout"


def test_run_job_first_output_timeout_disarms_after_first_byte(tmp_path, monkeypatch):
    """Once the workload produces a single byte, the first-output watchdog
    must disarm permanently — subsequent silence is the idle_timeout's job.
    Without this disarming, the watchdog would double-fire and kill jobs
    that legitimately quiet down after a startup banner."""
    import subprocess

    monkeypatch.setenv("JOBD_WORKER_FIRST_OUTPUT_TIMEOUT_S", "1")
    monkeypatch.setattr(job_worker, "SIGNAL_POLL_INTERVAL_S", 0.05)
    monkeypatch.setattr(job_worker.shutil, "which", lambda _name: None)  # fast-path

    # Stub stdout: one chunk immediately, then EOF after a short wait.
    yielded = threading.Event()
    finished = threading.Event()

    class _StubStdout:
        def __init__(self):
            self._yielded = False

        def read(self, _n):
            if not self._yielded:
                self._yielded = True
                yielded.set()
                return b"startup banner\n"
            finished.wait(timeout=3.0)
            return b""

        def close(self):
            pass

    class _StubProc:
        def __init__(self, _cmd, **_kw):
            self.pid = 44444
            self.stdout = _StubStdout()

        def send_signal(self, _sig):
            finished.set()

        def wait(self, timeout=None):
            finished.wait(timeout=3.0)
            return 0

        def kill(self):
            finished.set()

    monkeypatch.setattr(subprocess, "Popen", _StubProc)

    job = {"id": 9402, "cmd": ["echo", "x"], "cwd": str(tmp_path), "fast_path": True}
    client = _FakeClient(cancel_after_s=999.0)

    # Let the first-output watchdog window elapse — but the byte from
    # _StubStdout lands first, so disarm should fire and the workload runs
    # to clean EOF.
    def _release_after_disarm():
        yielded.wait(timeout=3.0)
        time.sleep(2.0)  # well past first_output_timeout_s=1
        finished.set()

    t = threading.Thread(target=_release_after_disarm, daemon=True)
    t.start()
    run_job(client, job, set())

    completes = [p for p in client.posts if p[0].endswith("/complete")]
    assert len(completes) == 1
    body = completes[0][1]
    # No watchdog fired → either no termination_reason, or definitely not
    # first_output_timeout.
    assert body.get("termination_reason") != "first_output_timeout"
    assert body["final_state"] in ("completed", "failed")  # depends on rc
