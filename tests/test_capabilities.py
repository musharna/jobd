"""Unit tests for worker capability detection."""

from pathlib import Path
from unittest.mock import patch


# Add worker/ to path for direct import
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))

from capabilities import detect  # noqa: E402


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
