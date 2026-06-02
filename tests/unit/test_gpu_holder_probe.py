"""S5 (runtime-zombies audit): dual-signal GPU-holder probe.

# METRIC_REFERENCE_OK - this is not a metric/probe formula test; it tests
# a diagnostic surface that unions NVML and fuser outputs. The "reference"
# is the new module src/jobd/gpu_holder_probe.py introduced in this commit.

NVML returns [N/A] for some held-GPU processes (driver/container/
permission edge cases). The desktop inference_server is the canonical
example: nvidia-smi listed the PID with N/A memory; only `fuser
-v /dev/nvidia*` surfaced it.

This probe unions NVML compute-apps with fuser-of-/dev/nvidia* so the
operator gets both signals merged into one (pid, gpu_id, mem_mb, source)
tuple list.
"""

from __future__ import annotations

from unittest.mock import patch


from jobd import gpu_holder_probe


def test_probe_returns_empty_when_no_signals():
    """No NVML data + no fuser hits → empty list, not an exception."""
    with patch.object(gpu_holder_probe, "_nvml_processes", return_value=[]):
        with patch.object(gpu_holder_probe, "_fuser_nvidia_pids", return_value=set()):
            result = gpu_holder_probe.probe_gpu_holders()
    assert result == []


def test_probe_emits_nvml_only_holder():
    nvml = [(1234, 0, 8192)]  # pid, gpu_id, mem_mb
    with patch.object(gpu_holder_probe, "_nvml_processes", return_value=nvml):
        with patch.object(gpu_holder_probe, "_fuser_nvidia_pids", return_value=set()):
            result = gpu_holder_probe.probe_gpu_holders()
    assert len(result) == 1
    h = result[0]
    assert h.pid == 1234
    assert h.gpu_id == 0
    assert h.mem_mb == 8192
    assert h.source == "nvml"


def test_probe_emits_fuser_only_holder():
    """NVML returns nothing but fuser sees /dev/nvidia* holders — they
    still surface (the desktop inference_server failure mode)."""
    with patch.object(gpu_holder_probe, "_nvml_processes", return_value=[]):
        with patch.object(gpu_holder_probe, "_fuser_nvidia_pids", return_value={9876}):
            result = gpu_holder_probe.probe_gpu_holders()
    assert len(result) == 1
    h = result[0]
    assert h.pid == 9876
    assert h.gpu_id is None
    assert h.mem_mb is None
    assert h.source == "fuser"


def test_probe_unions_overlapping_signals():
    """PID seen in both NVML and fuser → source='both', NVML's mem
    reading wins."""
    nvml = [(1234, 0, 4096)]
    with patch.object(gpu_holder_probe, "_nvml_processes", return_value=nvml):
        with patch.object(gpu_holder_probe, "_fuser_nvidia_pids", return_value={1234, 5555}):
            result = gpu_holder_probe.probe_gpu_holders()
    by_pid = {h.pid: h for h in result}
    assert by_pid[1234].source == "both"
    assert by_pid[1234].mem_mb == 4096
    assert by_pid[5555].source == "fuser"
    assert by_pid[5555].mem_mb is None


def test_probe_handles_nvml_unavailable():
    """No pynvml / no devices → falls through cleanly, fuser still runs."""
    with patch.object(gpu_holder_probe, "_nvml_processes", return_value=[]):
        with patch.object(gpu_holder_probe, "_fuser_nvidia_pids", return_value={42}):
            result = gpu_holder_probe.probe_gpu_holders()
    assert len(result) == 1
    assert result[0].pid == 42
    assert result[0].source == "fuser"


def test_probe_sorts_by_pid():
    """Stable output order: PIDs sorted ascending."""
    nvml = [(300, 0, 100), (100, 0, 200)]
    with patch.object(gpu_holder_probe, "_nvml_processes", return_value=nvml):
        with patch.object(gpu_holder_probe, "_fuser_nvidia_pids", return_value={200}):
            result = gpu_holder_probe.probe_gpu_holders()
    pids = [h.pid for h in result]
    assert pids == sorted(pids)


def test_fuser_nvidia_pids_parses_output(monkeypatch):
    """Verify fuser stderr parsing pulls out PIDs correctly. fuser -v
    writes to stderr in the form:
        USER PID ACCESS COMMAND
        root 1234 F.... python
    Some hosts emit numeric-only stdout instead; cover both.
    """
    sample = "                     USER        PID ACCESS COMMAND\n/dev/nvidia0:        root      12345 F.... python\nroot      67890 F.... torch\n"

    class _R:
        returncode = 0
        stdout = ""
        stderr = sample

    monkeypatch.setattr(gpu_holder_probe.subprocess, "run", lambda *a, **kw: _R())
    # Force the device-list to be non-empty so we attempt parsing.
    monkeypatch.setattr(gpu_holder_probe, "_nvidia_dev_nodes", lambda: ["/dev/nvidia0"])
    # Isolate the PARSER from the live /proc second-pass filter: force every
    # /proc/<pid>/comm read to miss so the function returns the raw parsed set.
    # Without this the test is flaky — it silently drops a synthetic pid whenever
    # that integer happens to be a live process on the host (real-execution leak).
    import builtins

    _real_open = builtins.open

    def _no_proc(path, *a, **k):
        if str(path).startswith("/proc/"):
            raise FileNotFoundError(path)
        return _real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", _no_proc)
    pids = gpu_holder_probe._fuser_nvidia_pids()
    assert 12345 in pids
    assert 67890 in pids


def test_fuser_returns_empty_when_no_dev_nodes(monkeypatch):
    """No /dev/nvidia* devices (WSL, no driver) → empty set, no fuser call."""
    monkeypatch.setattr(gpu_holder_probe, "_nvidia_dev_nodes", lambda: [])
    pids = gpu_holder_probe._fuser_nvidia_pids()
    assert pids == set()


def test_fuser_handles_missing_binary(monkeypatch):
    """fuser not on PATH → empty set, not a crash."""
    monkeypatch.setattr(gpu_holder_probe.shutil, "which", lambda _: None)
    monkeypatch.setattr(gpu_holder_probe, "_nvidia_dev_nodes", lambda: ["/dev/nvidia0"])
    pids = gpu_holder_probe._fuser_nvidia_pids()
    assert pids == set()


def test_broker_gpu_holders_endpoint(tmp_path, monkeypatch):
    """The broker /gpu-holders endpoint serializes the probe output to a
    JSON list. Patch the probe to a known shape so we don't depend on
    test-host driver state."""
    from fastapi.testclient import TestClient

    from jobd.app import build_app

    projects = tmp_path / "projects.yaml"
    projects.write_text("projects:\n  _default: { priority: 40 }\n")
    profiles = tmp_path / "profiles.yaml"
    profiles.write_text("profiles: {}\n")
    classifier = tmp_path / "classifier.yaml"
    classifier.write_text("rules: []\n")
    app = build_app(
        db_url=f"sqlite:///{tmp_path}/jobd.db",
        projects_path=projects,
        profiles_path=profiles,
        classifier_path=classifier,
        logs_path=tmp_path / "logs",
    )

    fake = [
        gpu_holder_probe.GpuHolder(pid=111, gpu_id=0, mem_mb=512, source="both", known=False),
        gpu_holder_probe.GpuHolder(pid=222, gpu_id=None, mem_mb=None, source="fuser", known=False),
    ]
    # Patch in the module where the broker imports it (jobd.app's local
    # import resolves to jobd.gpu_holder_probe). probe is invoked with
    # known_pids kwarg now; accept *args/**kwargs to stay version-agnostic.
    monkeypatch.setattr(gpu_holder_probe, "probe_gpu_holders", lambda *a, **kw: fake)
    client = TestClient(app)
    r = client.get("/gpu-holders")
    assert r.status_code == 200
    rows = r.json()
    assert rows == [
        {"pid": 111, "gpu_id": 0, "mem_mb": 512, "source": "both", "known": False},
        {"pid": 222, "gpu_id": None, "mem_mb": None, "source": "fuser", "known": False},
    ]


def test_probe_tags_known_pids_when_supplied():
    """S5 spec-review fix: when caller passes `known_pids`, each
    GpuHolder.known reflects set membership; full union still returned."""
    nvml = [(1234, 0, 4096), (5678, 0, 1024)]
    with patch.object(gpu_holder_probe, "_nvml_processes", return_value=nvml):
        with patch.object(gpu_holder_probe, "_fuser_nvidia_pids", return_value={9999}):
            result = gpu_holder_probe.probe_gpu_holders(known_pids={1234, 9999})
    by_pid = {h.pid: h for h in result}
    assert by_pid[1234].known is True, "1234 supplied in known_pids — must tag True"
    assert by_pid[5678].known is False, "5678 not in known_pids — tag False"
    assert by_pid[9999].known is True, "9999 supplied via known_pids — tag True"
    # Full union preserved — the consumer filters, not the probe.
    assert set(by_pid.keys()) == {1234, 5678, 9999}


def test_probe_treats_none_known_pids_as_all_unknown():
    """S5 spec-review fix: when `known_pids` is None (caller has no
    inventory), every row is tagged `known=False`. Matches the broker
    endpoint's current behavior — known_pids=empty until the Job DB
    grows a per-job PID column."""
    nvml = [(100, 0, 100), (200, 0, 200)]
    with patch.object(gpu_holder_probe, "_nvml_processes", return_value=nvml):
        with patch.object(gpu_holder_probe, "_fuser_nvidia_pids", return_value={300}):
            result = gpu_holder_probe.probe_gpu_holders(known_pids=None)
    assert all(h.known is False for h in result), [(h.pid, h.known) for h in result]
    # And the default (no arg) is the same.
    with patch.object(gpu_holder_probe, "_nvml_processes", return_value=nvml):
        with patch.object(gpu_holder_probe, "_fuser_nvidia_pids", return_value={300}):
            result2 = gpu_holder_probe.probe_gpu_holders()
    assert all(h.known is False for h in result2)
