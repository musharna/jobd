"""Issue #7: per-job PID inventory feeding /gpu-holders `known`.

Workers report {job_id: [pids]} in every heartbeat (top-level Popen pid plus
live scope-cgroup pids — same ownership boundary _effective_owned_pids uses).
The broker stores the latest map per worker and /gpu-holders resolves each
probed pid against it: `known=True` plus `job_id`/`worker` attribution.
PIDs are host-local, so the endpoint takes an optional ?host= filter; the
default consults every worker's inventory (documented collision caveat —
the endpoint is broker-local and primarily useful on a broker+worker host).
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

import jobd.worker.job_worker as job_worker
from jobd.app import build_app
from jobd.gpu_holder_probe import GpuHolder

# ---- worker side ---------------------------------------------------------


def _reset_worker_registries():
    with job_worker._in_flight_lock:
        job_worker._in_flight.clear()
    with job_worker._in_flight_pids_lock:
        job_worker._in_flight_pids.clear()


def test_in_flight_pid_map_unions_proc_pid_and_scope_pids(tmp_path, monkeypatch):
    _reset_worker_registries()
    try:
        scope_dir = tmp_path / "jobd-7001.scope"
        scope_dir.mkdir()
        (scope_dir / "cgroup.procs").write_text("4242\n4243\n")
        monkeypatch.setattr(job_worker, "_REAPER_OK", True)
        monkeypatch.setattr(
            job_worker._cgroup_walk,
            "resolve_user_scope_path",
            lambda unit: scope_dir if unit == "jobd-7001.scope" else None,
        )
        job_worker._register_in_flight_pid(7001, 999)
        assert job_worker._in_flight_pid_map() == {"7001": [999, 4242, 4243]}
    finally:
        _reset_worker_registries()


def test_in_flight_pid_map_without_scope_is_just_proc_pid(monkeypatch):
    _reset_worker_registries()
    try:
        monkeypatch.setattr(job_worker, "_REAPER_OK", True)
        monkeypatch.setattr(job_worker._cgroup_walk, "resolve_user_scope_path", lambda _u: None)
        job_worker._register_in_flight_pid(7002, 777)
        assert job_worker._in_flight_pid_map() == {"7002": [777]}
        job_worker._unregister_in_flight_pid(7002)
        assert job_worker._in_flight_pid_map() == {}
    finally:
        _reset_worker_registries()


def test_resource_snapshot_reports_in_flight_pids(monkeypatch):
    _reset_worker_registries()
    try:
        monkeypatch.setattr(job_worker, "nvidia_free_vram_gb", lambda: 30.0)
        monkeypatch.setattr(job_worker, "nvidia_processes", lambda: [])
        monkeypatch.setattr(job_worker._cgroup_walk, "resolve_user_scope_path", lambda _u: None)
        snap = job_worker.resource_snapshot(set())
        assert snap["in_flight_pids"] == {}
        job_worker._register_in_flight_pid(7003, 555)
        snap = job_worker.resource_snapshot(set())
        assert snap["in_flight_pids"] == {"7003": [555]}
    finally:
        _reset_worker_registries()


def test_run_job_registers_pid_during_run_and_unregisters_after(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setattr(job_worker.shutil, "which", lambda _n: None)
    _reset_worker_registries()
    release = threading.Event()

    class _StubStdout:
        def read(self, _n):
            release.wait(timeout=10.0)
            return b""

        def close(self):
            pass

    class _StubProc:
        def __init__(self, _cmd, **_kw):
            self.pid = 66666
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

    class _Client:
        def get(self, _p, **_k):
            class R:
                status_code = 200

                def json(self):
                    return {"signal": None}

            return R()

        def post(self, _p, **_k):
            class R:
                status_code = 200

                def json(self):
                    return {}

            return R()

    job = {"id": 7004, "cmd": ["bash", "-c", "sleep 30"], "cwd": str(tmp_path)}
    t = threading.Thread(target=job_worker.run_job, args=(_Client(), job, set()), daemon=True)
    t.start()
    try:
        deadline = 5.0
        import time as _time

        t0 = _time.monotonic()
        while _time.monotonic() - t0 < deadline:
            with job_worker._in_flight_pids_lock:
                if job_worker._in_flight_pids.get(7004) == 66666:
                    break
            _time.sleep(0.02)
        else:
            raise AssertionError("pid never registered")
    finally:
        release.set()
        t.join(5)
    with job_worker._in_flight_pids_lock:
        assert 7004 not in job_worker._in_flight_pids


# ---- broker side ---------------------------------------------------------


@pytest.fixture
def client(tmp_path, sample_projects_yaml, sample_profiles_yaml, sample_classifier_yaml):
    app = build_app(
        db_url=f"sqlite:///{tmp_path}/jobd.db",
        projects_path=sample_projects_yaml,
        profiles_path=sample_profiles_yaml,
        classifier_path=sample_classifier_yaml,
        logs_path=tmp_path / "logs",
    )
    return TestClient(app)


def _hb(client: TestClient, host: str, in_flight_pids: dict | None = None):
    body: dict = {
        "host": host,
        "free_vram_gb": 0,
        "unregistered_vram_gb": 0,
        "free_ram_gb": 8,
        "idle_cpus": 4,
    }
    if in_flight_pids is not None:
        body["in_flight_pids"] = in_flight_pids
    r = client.post("/heartbeat", json=body)
    assert r.status_code == 200, r.text
    return r


def _fake_probe(monkeypatch, pids: list[int]):
    holders = [GpuHolder(pid=p, gpu_id=0, mem_mb=1024, source="nvml", known=False) for p in pids]

    def fake(known_pids=None):
        known = set(known_pids or ())
        return [
            GpuHolder(
                pid=h.pid, gpu_id=h.gpu_id, mem_mb=h.mem_mb, source=h.source, known=h.pid in known
            )
            for h in holders
        ]

    monkeypatch.setattr("jobd.gpu_holder_probe.probe_gpu_holders", fake)


def test_gpu_holders_tags_known_pids_with_job_and_worker(client, monkeypatch):
    _fake_probe(monkeypatch, [555, 9999])
    _hb(client, "gpubox", in_flight_pids={"41": [555, 556]})
    rows = client.get("/gpu-holders").json()
    by_pid = {r["pid"]: r for r in rows}
    assert by_pid[555]["known"] is True
    assert by_pid[555]["job_id"] == 41
    assert by_pid[555]["worker"] == "gpubox"
    assert by_pid[9999]["known"] is False
    assert by_pid[9999]["job_id"] is None
    assert by_pid[9999]["worker"] is None


def test_gpu_holders_host_filter_scopes_inventory(client, monkeypatch):
    _fake_probe(monkeypatch, [555])
    _hb(client, "gpubox", in_flight_pids={"41": [555]})
    _hb(client, "otherbox", in_flight_pids={"77": [555]})  # same pid, other host
    rows = client.get("/gpu-holders", params={"host": "otherbox"}).json()
    assert rows[0]["known"] is True
    assert rows[0]["job_id"] == 77
    rows = client.get("/gpu-holders", params={"host": "no-such-host"}).json()
    assert rows[0]["known"] is False


def test_gpu_holders_unknown_without_any_inventory(client, monkeypatch):
    """Old workers don't report in_flight_pids; behavior matches pre-#7."""
    _fake_probe(monkeypatch, [555])
    _hb(client, "legacybox")  # no in_flight_pids field
    rows = client.get("/gpu-holders").json()
    assert rows[0]["known"] is False
    assert rows[0]["job_id"] is None
