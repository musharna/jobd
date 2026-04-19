"""Self-describe worker capabilities for the jobd broker.

Detects:
- arch (x86_64 / arm64 / arm7 / etc.)
- os (linux / darwin / windows)
- gpu (NVIDIA present)
- tags (cuda, nvidia-smi, python3, R, docker, ffmpeg, wsl, always-on, low-power)

Layered overrides (later wins):
  auto-detect  <  $JOBD_WORKER_TAGS (appends)  <  ~/.config/jobd/worker.yaml
"""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml


_POWER_SUPPLY_ROOT = Path("/sys/class/power_supply")

_ARCH_MAP = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv7l": "arm7",
    "arm": "arm7",
}


@dataclass
class Capabilities:
    arch: str = "unknown"
    os: str = "unknown"
    gpu: bool = False
    tags: list[str] = field(default_factory=list)


def _has_nvidia() -> bool:
    if shutil.which("nvidia-smi"):
        return True
    try:
        return Path("/proc/driver/nvidia/version").exists()
    except OSError:
        return False


def _wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def _on_battery() -> bool | None:
    """Return True if any battery reports Discharging, False if none are, None if unknown."""
    try:
        if not _POWER_SUPPLY_ROOT.exists():
            return None
        found_any = False
        for p in _POWER_SUPPLY_ROOT.iterdir():
            type_f = p / "type"
            if type_f.exists() and type_f.read_text().strip() == "Battery":
                status_f = p / "status"
                if not status_f.exists():
                    return None
                if status_f.read_text().strip() == "Discharging":
                    return True
                found_any = True
        return False if found_any else None
    except OSError:
        return None


def _auto_tags() -> list[str]:
    tags: list[str] = []
    for tool in ("python3", "R", "docker", "ffmpeg", "nvidia-smi"):
        if shutil.which(tool):
            tags.append(tool)
    py_ver = f"python{platform.python_version_tuple()[0]}.{platform.python_version_tuple()[1]}"
    if py_ver not in tags:
        tags.append(py_ver)
    if _has_nvidia():
        if "cuda" not in tags:
            tags.append("cuda")
    if _wsl():
        tags.append("wsl")
    battery = _on_battery()
    if battery is False:
        tags.append("always-on")
    elif battery is True:
        tags.append("low-power")
    return tags


def _env_extra_tags() -> list[str]:
    raw = os.environ.get("JOBD_WORKER_TAGS", "")
    return [t.strip() for t in raw.split(",") if t.strip()]


def _config_path() -> Path | None:
    envp = os.environ.get("JOBD_WORKER_CONFIG")
    if envp:
        return Path(envp)
    default = Path.home() / ".config" / "jobd" / "worker.yaml"
    return default if default.exists() else None


def _load_config_overrides() -> dict:
    p = _config_path()
    if p is None:
        return {}
    try:
        return yaml.safe_load(p.read_text()) or {}
    except (OSError, yaml.YAMLError, ValueError):
        return {}


def detect() -> Capabilities:
    """Return the current host's capabilities, honoring overrides."""
    raw_machine = platform.machine().lower()
    arch = _ARCH_MAP.get(raw_machine, raw_machine or "unknown")
    system = platform.system().lower()  # linux / darwin / windows
    if system not in ("linux", "darwin", "windows"):
        system = "unknown"
    gpu = _has_nvidia()
    tags = _auto_tags()

    # env var appends
    for t in _env_extra_tags():
        if t not in tags:
            tags.append(t)

    # config-file overrides (arch, os, gpu, tags — replaces if provided)
    ov = _load_config_overrides()
    if isinstance(ov, dict):
        if "arch" in ov:
            arch = str(ov["arch"])
        if "os" in ov:
            system = str(ov["os"])
        if "gpu" in ov:
            gpu = bool(ov["gpu"])
        if "tags" in ov and isinstance(ov["tags"], list):
            tags = [str(t) for t in ov["tags"]]

    return Capabilities(arch=arch, os=system, gpu=gpu, tags=tags)
