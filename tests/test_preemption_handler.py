"""Workload-side preemption helper (jobd.client.install_preemption_handler)."""

from __future__ import annotations

import os
import signal as sig_mod
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jobd.client import (
    CHECKPOINT_COMPLETE_TOKEN,
    install_preemption_handler,
    time_remaining,
)


def test_time_remaining_reads_env_grace(monkeypatch):
    monkeypatch.setenv("JOBD_CHECKPOINT_GRACE_S", "120")
    install_preemption_handler(lambda _t: None)
    assert time_remaining() == pytest.approx(120.0)


def test_time_remaining_default_when_env_missing(monkeypatch):
    monkeypatch.delenv("JOBD_CHECKPOINT_GRACE_S", raising=False)
    install_preemption_handler(lambda _t: None)
    assert time_remaining() == pytest.approx(60.0)


def test_time_remaining_default_when_env_garbage(monkeypatch):
    monkeypatch.setenv("JOBD_CHECKPOINT_GRACE_S", "not-a-number")
    install_preemption_handler(lambda _t: None)
    assert time_remaining() == pytest.approx(60.0)


def _run_handler_subprocess(
    tmp_path: Path, checkpoint_body: str, env_overrides: dict[str, str]
) -> subprocess.CompletedProcess:
    """Spawn a child python that installs the handler, sends itself
    SIGTERM, and we collect its stdout."""
    src_dir = Path(__file__).resolve().parent.parent / "src"
    child = tmp_path / "child.py"
    child.write_text(
        "import time\n"
        "from jobd.client import install_preemption_handler, time_remaining\n"
        f"{checkpoint_body}\n"
        "install_preemption_handler(_cp)\n"
        "print('READY', flush=True)\n"
        "time.sleep(30)\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(src_dir) + os.pathsep + env.get("PYTHONPATH", "")
    env.update(env_overrides)
    proc = subprocess.Popen(
        [sys.executable, str(child)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    # Wait for child to print READY so we know the handler is installed.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        line = proc.stdout.readline() if proc.stdout else ""
        if line.strip() == "READY":
            break
        if not line:
            time.sleep(0.05)
    proc.send_signal(sig_mod.SIGTERM)
    out, _ = proc.communicate(timeout=10)
    return subprocess.CompletedProcess(
        args=proc.args,
        returncode=proc.returncode,
        stdout=("READY\n" if "READY" not in out else "") + out,
        stderr="",
    )


def test_handler_prints_token_and_exits_zero_on_success(tmp_path):
    """Successful checkpoint_fn → token printed, exit 0."""
    # checkpoint_fn runs in signal-handler context, so it uses os.write (the
    # async-signal-safe pattern jobd documents) rather than buffered print.
    body = (
        "import os\n"
        "_seen = []\n"
        "def _cp(remaining):\n"
        "    _seen.append(remaining)\n"
        "    os.write(1, f'TR={remaining:.1f}\\n'.encode())\n"
    )
    result = _run_handler_subprocess(tmp_path, body, {"JOBD_CHECKPOINT_GRACE_S": "30"})
    assert CHECKPOINT_COMPLETE_TOKEN in result.stdout
    assert result.returncode == 0
    # time_remaining was visible to the user fn.
    assert "TR=" in result.stdout


def test_handler_skips_token_and_exits_one_on_checkpoint_failure(tmp_path):
    """checkpoint_fn raising → no token, exit 1, error visible in output."""
    body = "def _cp(remaining):\n    raise RuntimeError('disk full')\n"
    result = _run_handler_subprocess(tmp_path, body, {"JOBD_CHECKPOINT_GRACE_S": "30"})
    assert CHECKPOINT_COMPLETE_TOKEN not in result.stdout
    assert result.returncode == 1
    assert "disk full" in result.stdout


def test_handler_works_without_jobd_env(tmp_path):
    """Outside a jobd context (env var unset) the handler still works
    so dev scripts can opt in early — defaults to 60s."""
    body = "def _cp(remaining):\n    os.write(1, f'TR={remaining:.1f}\\n'.encode())\n"
    env = {k: v for k, v in os.environ.items() if k != "JOBD_CHECKPOINT_GRACE_S"}
    src_dir = Path(__file__).resolve().parent.parent / "src"
    child = tmp_path / "child.py"
    child.write_text(
        "import os, time\n"
        "os.environ.pop('JOBD_CHECKPOINT_GRACE_S', None)\n"
        "from jobd.client import install_preemption_handler, time_remaining\n"
        f"{body}\n"
        "install_preemption_handler(_cp)\n"
        "print('READY', flush=True)\n"
        "time.sleep(30)\n"
    )
    env["PYTHONPATH"] = str(src_dir) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, str(child)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        line = proc.stdout.readline() if proc.stdout else ""
        if line.strip() == "READY":
            break
        if not line:
            time.sleep(0.05)
    proc.send_signal(sig_mod.SIGTERM)
    out, _ = proc.communicate(timeout=10)
    assert "TR=60.0" in out
    assert CHECKPOINT_COMPLETE_TOKEN in out
    assert proc.returncode == 0
