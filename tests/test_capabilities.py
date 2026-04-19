"""Unit tests for worker capability detection."""

from pathlib import Path
from unittest.mock import patch


# Add worker/ to path for direct import
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))

from capabilities import Capabilities, detect  # noqa: E402


def test_detect_arch_x86_64():
    with (
        patch("capabilities.platform.machine", return_value="x86_64"),
        patch("capabilities.platform.system", return_value="Linux"),
        patch("capabilities.shutil.which", return_value=None),
        patch("capabilities._has_nvidia", return_value=False),
        patch("capabilities._wsl", return_value=False),
        patch.dict("os.environ", {}, clear=False),
    ):
        c = detect()
    assert c.arch == "x86_64"
    assert c.os == "linux"
    assert c.gpu is False
    assert isinstance(c, Capabilities)


def test_detect_arm64_linux_no_gpu():
    with (
        patch("capabilities.platform.machine", return_value="aarch64"),
        patch("capabilities.platform.system", return_value="Linux"),
        patch("capabilities.shutil.which", return_value=None),
        patch("capabilities._has_nvidia", return_value=False),
        patch("capabilities._wsl", return_value=False),
    ):
        c = detect()
    assert c.arch == "arm64"
    assert c.os == "linux"
    assert c.gpu is False


def test_detect_gpu_adds_cuda_tag():
    def _which(name: str):
        return f"/usr/bin/{name}" if name in ("nvidia-smi", "python3") else None

    with (
        patch("capabilities.platform.machine", return_value="x86_64"),
        patch("capabilities.platform.system", return_value="Linux"),
        patch("capabilities.shutil.which", side_effect=_which),
        patch("capabilities._has_nvidia", return_value=True),
        patch("capabilities._wsl", return_value=False),
    ):
        c = detect()
    assert c.gpu is True
    assert "cuda" in c.tags
    assert "nvidia-smi" in c.tags


def test_detect_wsl_sets_tag():
    with (
        patch("capabilities.platform.machine", return_value="x86_64"),
        patch("capabilities.platform.system", return_value="Linux"),
        patch("capabilities.shutil.which", return_value=None),
        patch("capabilities._has_nvidia", return_value=False),
        patch("capabilities._wsl", return_value=True),
    ):
        c = detect()
    assert "wsl" in c.tags


def test_env_override_appends_tags():
    with (
        patch("capabilities.platform.machine", return_value="x86_64"),
        patch("capabilities.platform.system", return_value="Linux"),
        patch("capabilities.shutil.which", return_value=None),
        patch("capabilities._has_nvidia", return_value=False),
        patch("capabilities._wsl", return_value=False),
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
        patch("capabilities.platform.machine", return_value="x86_64"),
        patch("capabilities.platform.system", return_value="Linux"),
        patch("capabilities.shutil.which", return_value=None),
        patch("capabilities._has_nvidia", return_value=False),
        patch("capabilities._wsl", return_value=False),
    ):
        c = detect()
    assert c.arch == "arm7"
    assert "custom-tag" in c.tags


# --- Battery detection tests (Fix 1) ---


def test_on_battery_discharging(tmp_path, monkeypatch):
    bat = tmp_path / "BAT0"
    bat.mkdir()
    (bat / "type").write_text("Battery\n")
    (bat / "status").write_text("Discharging\n")
    import capabilities as caps_mod

    monkeypatch.setattr(caps_mod, "_POWER_SUPPLY_ROOT", tmp_path)
    assert caps_mod._on_battery() is True


def test_on_battery_not_discharging(tmp_path, monkeypatch):
    bat = tmp_path / "BAT0"
    bat.mkdir()
    (bat / "type").write_text("Battery\n")
    (bat / "status").write_text("Not charging\n")
    import capabilities as caps_mod

    monkeypatch.setattr(caps_mod, "_POWER_SUPPLY_ROOT", tmp_path)
    assert caps_mod._on_battery() is False


def test_on_battery_no_battery_dir(tmp_path, monkeypatch):
    import capabilities as caps_mod

    monkeypatch.setattr(caps_mod, "_POWER_SUPPLY_ROOT", tmp_path / "does-not-exist")
    assert caps_mod._on_battery() is None


# --- Malformed YAML test (Fix 2) ---


def test_malformed_yaml_does_not_crash(tmp_path, monkeypatch):
    cfg = tmp_path / "worker.yaml"
    cfg.write_text("arch: [unclosed list\nnot valid yaml:\n  - :")
    monkeypatch.setenv("JOBD_WORKER_CONFIG", str(cfg))
    with (
        patch("capabilities.platform.machine", return_value="x86_64"),
        patch("capabilities.platform.system", return_value="Linux"),
        patch("capabilities.shutil.which", return_value=None),
        patch("capabilities._has_nvidia", return_value=False),
        patch("capabilities._wsl", return_value=False),
    ):
        c = detect()
    assert c.arch == "x86_64"  # fell back to auto-detect, no crash


def test_on_battery_multi_battery_discharging_found_second(tmp_path, monkeypatch):
    """Two batteries: first is Full, second is Discharging → True."""
    import capabilities as caps_mod

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
        patch("capabilities.platform.machine", return_value="x86_64"),
        patch("capabilities.platform.system", return_value="Linux"),
        patch("capabilities.shutil.which", return_value=None),
        patch("capabilities._has_nvidia", return_value=False),
        patch("capabilities._wsl", return_value=False),
    ):
        c = detect()
    assert c.arch == "x86_64"
