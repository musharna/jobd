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
    if Path("/usr/lib/wsl/lib/nvidia-smi").exists():
        return True
    try:
        if Path("/proc/driver/nvidia/version").exists():
            return True
    except OSError:
        pass
    try:
        import pynvml

        pynvml.nvmlInit()
        pynvml.nvmlShutdown()
        return True
    except Exception:
        return False


# VRAM tier thresholds in GiB. A worker advertises every tier its total VRAM
# exceeds — so a 32 GB GPU advertises cuda + cuda-8gb + cuda-12gb + cuda-16gb
# + cuda-24gb + cuda-32gb. Jobs that just want "any CUDA" stay on `cuda`;
# jobs that need a specific minimum ask for `cuda-32gb` (or whichever tier).
# 8 GB is the floor so an RTX 2080-class GPU advertises a discoverable tier.
_CUDA_TIERS_GB = (8, 12, 16, 24, 32)


def _max_cuda_vram_gb() -> int:
    """Return total VRAM (GB, rounded to nearest int) of the largest visible
    NVIDIA GPU.

    Round-to-nearest, not floor: NVIDIA GPUs are sold in clean nominal sizes
    (12 / 16 / 24 / 32 GB) but the driver reserves a slice, so a 12 GB card
    reports ~11.6 GiB. Floor would push it below the cuda-12gb threshold and
    the GPU silently fails to advertise its own class. Rounding to 12 is the
    intuitive answer.

    Returns 0 if pynvml isn't available or no devices are visible.
    """
    try:
        import pynvml

        pynvml.nvmlInit()
        try:
            count = pynvml.nvmlDeviceGetCount()
            best = 0
            for i in range(count):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                gb = round(info.total / (1024**3))
                if gb > best:
                    best = gb
            return best
        finally:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass
    except Exception:
        return 0


def _cuda_tier_tags(total_gb: int) -> list[str]:
    """All cuda-Ngb tags whose threshold the GPU's total VRAM meets."""
    return [f"cuda-{t}gb" for t in _CUDA_TIERS_GB if total_gb >= t]


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
        # Granularity tags: a job that needs a specific VRAM floor can ask for
        # `cuda-32gb` instead of `cuda`. Without these, `needs:["cuda"]` would
        # match any CUDA host (incl. a 12 GB laptop GPU) — surprising when the
        # user thought of "CUDA work" as desktop-only by default.
        for t in _cuda_tier_tags(_max_cuda_vram_gb()):
            if t not in tags:
                tags.append(t)
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
