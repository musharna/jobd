"""Unit tests for worker capability detection."""

import sys
from pathlib import Path
from unittest.mock import patch

from jobd.worker.capabilities import Capabilities, detect


def test_detect_arch_x86_64():
    with (
        patch("jobd.worker.capabilities.platform.machine", return_value="x86_64"),
        patch("jobd.worker.capabilities.platform.system", return_value="Linux"),
        patch("jobd.worker.capabilities.shutil.which", return_value=None),
        patch("jobd.worker.capabilities._has_nvidia", return_value=False),
        patch("jobd.worker.capabilities._wsl", return_value=False),
        patch.dict("os.environ", {}, clear=False),
    ):
        c = detect()
    assert c.arch == "x86_64"
    assert c.os == "linux"
    assert c.gpu is False
    assert isinstance(c, Capabilities)


def test_detect_arm64_linux_no_gpu():
    with (
        patch("jobd.worker.capabilities.platform.machine", return_value="aarch64"),
        patch("jobd.worker.capabilities.platform.system", return_value="Linux"),
        patch("jobd.worker.capabilities.shutil.which", return_value=None),
        patch("jobd.worker.capabilities._has_nvidia", return_value=False),
        patch("jobd.worker.capabilities._wsl", return_value=False),
    ):
        c = detect()
    assert c.arch == "arm64"
    assert c.os == "linux"
    assert c.gpu is False


def test_detect_gpu_adds_cuda_tag():
    def _which(name: str):
        return f"/usr/bin/{name}" if name in ("nvidia-smi", "python3") else None

    with (
        patch("jobd.worker.capabilities.platform.machine", return_value="x86_64"),
        patch("jobd.worker.capabilities.platform.system", return_value="Linux"),
        patch("jobd.worker.capabilities.shutil.which", side_effect=_which),
        patch("jobd.worker.capabilities._has_nvidia", return_value=True),
        patch("jobd.worker.capabilities._wsl", return_value=False),
    ):
        c = detect()
    assert c.gpu is True
    assert "cuda" in c.tags
    assert "nvidia-smi" in c.tags


def test_detect_wsl_sets_tag():
    with (
        patch("jobd.worker.capabilities.platform.machine", return_value="x86_64"),
        patch("jobd.worker.capabilities.platform.system", return_value="Linux"),
        patch("jobd.worker.capabilities.shutil.which", return_value=None),
        patch("jobd.worker.capabilities._has_nvidia", return_value=False),
        patch("jobd.worker.capabilities._wsl", return_value=True),
    ):
        c = detect()
    assert "wsl" in c.tags


def test_env_override_appends_tags():
    with (
        patch("jobd.worker.capabilities.platform.machine", return_value="x86_64"),
        patch("jobd.worker.capabilities.platform.system", return_value="Linux"),
        patch("jobd.worker.capabilities.shutil.which", return_value=None),
        patch("jobd.worker.capabilities._has_nvidia", return_value=False),
        patch("jobd.worker.capabilities._wsl", return_value=False),
        patch.dict("os.environ", {"JOBD_WORKER_TAGS": "extra1,extra2"}),
    ):
        c = detect()
    assert "extra1" in c.tags
    assert "extra2" in c.tags


def test_config_file_override_replaces_arch(tmp_path, monkeypatch):
    cfg = tmp_path / "worker.yaml"
    cfg.write_text("arch: arm7\ntags: [custom-tag]\n")
    monkeypatch.setenv("JOBD_WORKER_CONFIG", str(cfg))
    with (
        patch("jobd.worker.capabilities.platform.machine", return_value="x86_64"),
        patch("jobd.worker.capabilities.platform.system", return_value="Linux"),
        patch("jobd.worker.capabilities.shutil.which", return_value=None),
        patch("jobd.worker.capabilities._has_nvidia", return_value=False),
        patch("jobd.worker.capabilities._wsl", return_value=False),
    ):
        c = detect()
    assert c.arch == "arm7"
    assert "custom-tag" in c.tags


def test_yaml_without_tags_preserves_autodetected_tier_tags(tmp_path, monkeypatch):
    """install-worker.sh deliberately doesn't write `tags:` (#51 fix).

    A yaml that omits `tags:` must let the runtime's auto-detected list
    through — including the cuda-Ngb tier tags computed from live VRAM —
    rather than blanking them out. Pre-fix install-worker wrote a static
    `tags:` block that REPLACED auto-detect on every start, killing the
    cuda-8gb tier tag for server. Regression-locking the post-fix behavior.
    """
    import jobd.worker.capabilities as caps_mod

    cfg = tmp_path / "worker.yaml"
    cfg.write_text("host: server\narch: x86_64\nos: linux\ngpu: true\n")
    monkeypatch.setenv("JOBD_WORKER_CONFIG", str(cfg))
    with (
        patch("jobd.worker.capabilities.platform.machine", return_value="x86_64"),
        patch("jobd.worker.capabilities.platform.system", return_value="Linux"),
        patch("jobd.worker.capabilities.shutil.which", return_value=None),
        patch("jobd.worker.capabilities._has_nvidia", return_value=True),
        patch("jobd.worker.capabilities._wsl", return_value=False),
        patch.object(caps_mod, "_max_cuda_vram_gb", return_value=8.5),
    ):
        c = detect()
    assert "cuda" in c.tags
    assert "cuda-8gb" in c.tags


# --- Battery detection tests (Fix 1) ---


def test_on_battery_discharging(tmp_path, monkeypatch):
    bat = tmp_path / "BAT0"
    bat.mkdir()
    (bat / "type").write_text("Battery\n")
    (bat / "status").write_text("Discharging\n")
    import jobd.worker.capabilities as caps_mod

    monkeypatch.setattr(caps_mod, "_POWER_SUPPLY_ROOT", tmp_path)
    assert caps_mod._on_battery() is True


def test_on_battery_not_discharging(tmp_path, monkeypatch):
    bat = tmp_path / "BAT0"
    bat.mkdir()
    (bat / "type").write_text("Battery\n")
    (bat / "status").write_text("Not charging\n")
    import jobd.worker.capabilities as caps_mod

    monkeypatch.setattr(caps_mod, "_POWER_SUPPLY_ROOT", tmp_path)
    assert caps_mod._on_battery() is False


def test_on_battery_no_battery_dir(tmp_path, monkeypatch):
    import jobd.worker.capabilities as caps_mod

    monkeypatch.setattr(caps_mod, "_POWER_SUPPLY_ROOT", tmp_path / "does-not-exist")
    assert caps_mod._on_battery() is None


# --- Malformed YAML test (Fix 2) ---


def test_malformed_yaml_does_not_crash(tmp_path, monkeypatch):
    cfg = tmp_path / "worker.yaml"
    cfg.write_text("arch: [unclosed list\nnot valid yaml:\n  - :")
    monkeypatch.setenv("JOBD_WORKER_CONFIG", str(cfg))
    with (
        patch("jobd.worker.capabilities.platform.machine", return_value="x86_64"),
        patch("jobd.worker.capabilities.platform.system", return_value="Linux"),
        patch("jobd.worker.capabilities.shutil.which", return_value=None),
        patch("jobd.worker.capabilities._has_nvidia", return_value=False),
        patch("jobd.worker.capabilities._wsl", return_value=False),
    ):
        c = detect()
    assert c.arch == "x86_64"  # fell back to auto-detect, no crash


def test_on_battery_multi_battery_discharging_found_second(tmp_path, monkeypatch):
    """Two batteries: first is Full, second is Discharging → True."""
    import jobd.worker.capabilities as caps_mod

    for name, status in [("BAT0", "Full"), ("BAT1", "Discharging")]:
        bat = tmp_path / name
        bat.mkdir()
        (bat / "type").write_text("Battery\n")
        (bat / "status").write_text(f"{status}\n")
    monkeypatch.setattr(caps_mod, "_POWER_SUPPLY_ROOT", tmp_path)
    assert caps_mod._on_battery() is True


def test_binary_config_file_does_not_crash(tmp_path, monkeypatch):
    """Non-UTF-8 bytes in worker.yaml fall back to auto-detect."""
    cfg = tmp_path / "worker.yaml"
    cfg.write_bytes(b"\xff\xfe\x00\x01\x02\x03\xc3\x28")  # invalid UTF-8
    monkeypatch.setenv("JOBD_WORKER_CONFIG", str(cfg))
    with (
        patch("jobd.worker.capabilities.platform.machine", return_value="x86_64"),
        patch("jobd.worker.capabilities.platform.system", return_value="Linux"),
        patch("jobd.worker.capabilities.shutil.which", return_value=None),
        patch("jobd.worker.capabilities._has_nvidia", return_value=False),
        patch("jobd.worker.capabilities._wsl", return_value=False),
    ):
        c = detect()
    assert c.arch == "x86_64"


def test_has_nvidia_detects_wsl_stub_path():
    """_has_nvidia returns True when /usr/lib/wsl/lib/nvidia-smi exists even if PATH lookup fails."""
    import jobd.worker.capabilities as caps_mod

    real_exists = Path.exists

    def fake_exists(self):
        if str(self) == "/usr/lib/wsl/lib/nvidia-smi":
            return True
        return real_exists(self)

    with (
        patch("jobd.worker.capabilities.shutil.which", return_value=None),
        patch("pathlib.Path.exists", fake_exists),
    ):
        assert caps_mod._has_nvidia() is True


def test_cuda_tier_tags_thresholds():
    """Tier tags are additive: every threshold the GPU exceeds gets emitted."""
    import jobd.worker.capabilities as caps_mod

    assert caps_mod._cuda_tier_tags(0) == []
    assert caps_mod._cuda_tier_tags(7) == []
    assert caps_mod._cuda_tier_tags(8) == ["cuda-8gb"]
    assert caps_mod._cuda_tier_tags(11) == ["cuda-8gb"]
    assert caps_mod._cuda_tier_tags(12) == ["cuda-8gb", "cuda-12gb"]
    assert caps_mod._cuda_tier_tags(15) == ["cuda-8gb", "cuda-12gb"]
    assert caps_mod._cuda_tier_tags(16) == ["cuda-8gb", "cuda-12gb", "cuda-16gb"]
    assert caps_mod._cuda_tier_tags(24) == [
        "cuda-8gb",
        "cuda-12gb",
        "cuda-16gb",
        "cuda-24gb",
    ]
    assert caps_mod._cuda_tier_tags(32) == [
        "cuda-8gb",
        "cuda-12gb",
        "cuda-16gb",
        "cuda-24gb",
        "cuda-32gb",
    ]


def test_detect_emits_cuda_tier_tags_for_32gb():
    """A 32 GB CUDA host (e.g. RTX 5090) emits cuda + every tier up to 32."""

    def _which(name: str):
        return f"/usr/bin/{name}" if name in ("nvidia-smi", "python3") else None

    with (
        patch("jobd.worker.capabilities.platform.machine", return_value="x86_64"),
        patch("jobd.worker.capabilities.platform.system", return_value="Linux"),
        patch("jobd.worker.capabilities.shutil.which", side_effect=_which),
        patch("jobd.worker.capabilities._has_nvidia", return_value=True),
        patch("jobd.worker.capabilities._max_cuda_vram_gb", return_value=32),
        patch("jobd.worker.capabilities._wsl", return_value=False),
    ):
        c = detect()
    assert "cuda" in c.tags
    assert "cuda-12gb" in c.tags
    assert "cuda-16gb" in c.tags
    assert "cuda-24gb" in c.tags
    assert "cuda-32gb" in c.tags


def test_detect_emits_only_low_tier_for_12gb():
    """A 12 GB CUDA host (e.g. RTX 4080 laptop) emits cuda + cuda-12gb only —
    NOT cuda-32gb. Jobs that ask for cuda-32gb must NOT route here. This is
    the operational fix for the 2026-04-26 'cuda routes to laptop' surprise."""

    def _which(name: str):
        return f"/usr/bin/{name}" if name in ("nvidia-smi", "python3") else None

    with (
        patch("jobd.worker.capabilities.platform.machine", return_value="x86_64"),
        patch("jobd.worker.capabilities.platform.system", return_value="Linux"),
        patch("jobd.worker.capabilities.shutil.which", side_effect=_which),
        patch("jobd.worker.capabilities._has_nvidia", return_value=True),
        patch("jobd.worker.capabilities._max_cuda_vram_gb", return_value=12),
        patch("jobd.worker.capabilities._wsl", return_value=False),
    ):
        c = detect()
    assert "cuda" in c.tags
    assert "cuda-12gb" in c.tags
    assert "cuda-16gb" not in c.tags
    assert "cuda-24gb" not in c.tags
    assert "cuda-32gb" not in c.tags


def test_detect_no_tier_tags_when_pynvml_unavailable():
    """If _max_cuda_vram_gb returns 0 (pynvml absent / not initialized), the
    bare `cuda` tag still ships but no tier tags do — be conservative; don't
    advertise a capability we can't measure."""

    def _which(name: str):
        return f"/usr/bin/{name}" if name in ("nvidia-smi", "python3") else None

    with (
        patch("jobd.worker.capabilities.platform.machine", return_value="x86_64"),
        patch("jobd.worker.capabilities.platform.system", return_value="Linux"),
        patch("jobd.worker.capabilities.shutil.which", side_effect=_which),
        patch("jobd.worker.capabilities._has_nvidia", return_value=True),
        patch("jobd.worker.capabilities._max_cuda_vram_gb", return_value=0),
        patch("jobd.worker.capabilities._wsl", return_value=False),
    ):
        c = detect()
    assert "cuda" in c.tags
    assert not any(t.startswith("cuda-") for t in c.tags)


def test_has_nvidia_falls_back_to_pynvml():
    """When no filesystem signal is present, a successful pynvml.nvmlInit is enough."""
    import types

    import jobd.worker.capabilities as caps_mod

    fake_pynvml = types.SimpleNamespace(
        nvmlInit=lambda: None,
        nvmlShutdown=lambda: None,
    )
    with (
        patch("jobd.worker.capabilities.shutil.which", return_value=None),
        patch("pathlib.Path.exists", lambda self: False),
        patch.dict(sys.modules, {"pynvml": fake_pynvml}),
    ):
        assert caps_mod._has_nvidia() is True
