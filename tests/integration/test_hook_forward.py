"""Integration test for hook → broker /events forwarding.

Drives the example examples/claude-code-hooks/jobd-block-gpu.sh hook with
stubbed `curl` and `ssh` on PATH and a synthetic Claude-Code stdin
payload. Asserts the hook POSTs schema-v2 envelopes to /events for each of
the six (event, outcome) pairs.

The hook no-ops unless JOBD_GPU_SSH and JOBD_GPU_HOST_PAT are set, so the
test supplies both (see _run_hook). The ssh probe is stubbed regardless.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

HOOK_PATH = (
    Path(__file__).resolve().parents[2] / "examples" / "claude-code-hooks" / "jobd-block-gpu.sh"
)

pytestmark = pytest.mark.skipif(
    not HOOK_PATH.exists(),
    reason=f"example hook not found at {HOOK_PATH}",
)

# Host the hook is configured to guard (matches JOBD_GPU_HOST_PAT below).
GPU_HOST = "gpu-host"
# A command that matches BOTH the hook's host regex AND a GPU-work regex.
# `nohup python ...` matches the `nohup[[:space:]]+[^|]*python` GPU pattern;
# `gpu-host` matches JOBD_GPU_HOST_PAT. Reused across tests for stable asserts.
TEST_CMD_BASE = f"ssh {GPU_HOST} 'nohup python train.py'"


def _make_stubs(stubs_dir: Path, capture_dir: Path) -> None:
    """Write curl + ssh stubs into stubs_dir."""
    capture_dir.mkdir(parents=True, exist_ok=True)
    curl = stubs_dir / "curl"
    curl.write_text(
        f"""#!/usr/bin/env bash
# Capture argv and the --data body for assertion.
printf '%s\\n' "$@" > {capture_dir}/curl_args
prev=""
for a in "$@"; do
    if [[ "$prev" == "--data" ]]; then
        printf '%s' "$a" > {capture_dir}/curl_body
        break
    fi
    prev="$a"
done
exit 0
"""
    )
    curl.chmod(0o755)

    ssh = stubs_dir / "ssh"
    ssh.write_text(
        """#!/usr/bin/env bash
# Differentiate the two probes by trailing remote-command content.
last="${@: -1}"
if [[ "$last" == *"compute-apps"* ]]; then
    [[ -n "${SSH_STUB_COMPUTE_APPS:-}" ]] && printf '%s' "$SSH_STUB_COMPUTE_APPS"
    exit "${SSH_STUB_COMPUTE_APPS_EXIT:-0}"
elif [[ "$last" == *"memory.free"* ]]; then
    [[ -n "${SSH_STUB_MEMORY:-}" ]] && printf '%s' "$SSH_STUB_MEMORY"
    exit "${SSH_STUB_MEMORY_EXIT:-0}"
fi
exit 0
"""
    )
    ssh.chmod(0o755)


def _run_hook(
    cmd: str,
    *,
    tmp_path: Path,
    jobd_url: str | None = "http://test.invalid",
    ssh_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess, Path, Path]:
    """Spawn the hook with stubs + controlled env. Returns (proc, tsv_path, capture_dir)."""
    stubs_dir = tmp_path / "stubs"
    stubs_dir.mkdir()
    capture_dir = tmp_path / "capture"
    _make_stubs(stubs_dir, capture_dir)

    tsv = tmp_path / "jobd-blocks.log"

    env = {
        "PATH": f"{stubs_dir}:{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path),
        "JOBD_BLOCK_LOG": str(tsv),
        # Activate the (otherwise no-op) example hook and point it at the stubbed host.
        "JOBD_GPU_SSH": f"user@{GPU_HOST}",
        "JOBD_GPU_HOST_PAT": GPU_HOST,
    }
    if jobd_url is not None:
        env["JOBD_URL"] = jobd_url
    if ssh_env:
        env.update(ssh_env)

    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
    proc = subprocess.run(
        ["bash", str(HOOK_PATH)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    # Hook backgrounds curl with `& disown`; wait briefly for the stub flush.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if (capture_dir / "curl_body").exists():
            break
        time.sleep(0.05)

    return proc, tsv, capture_dir


def _curl_body(capture_dir: Path) -> dict:
    return json.loads((capture_dir / "curl_body").read_text())


def test_hook_forward_no_bypass_marker(tmp_path):
    proc, tsv, cap = _run_hook(TEST_CMD_BASE, tmp_path=tmp_path)
    assert proc.returncode == 2, proc.stderr
    body = _curl_body(cap)
    assert body["source"] == "hook"
    assert body["event"] == "hook_blocked"
    assert body["payload"]["outcome"] == "no_bypass_marker"
    assert "nohup python train.py" in body["payload"]["cmd_oneline"]
    assert body["payload"]["host"]
    assert "\tBLOCK\t" in tsv.read_text()


def test_hook_forward_no_gpu_marker(tmp_path):
    cmd = TEST_CMD_BASE + " # NO_GPU"
    proc, tsv, cap = _run_hook(cmd, tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stderr
    body = _curl_body(cap)
    assert body["event"] == "hook_bypassed"
    assert body["payload"]["outcome"] == "no_gpu_explicit"
    assert "\tNO_GPU\t" in tsv.read_text()


def test_hook_forward_concurrent_ok(tmp_path):
    cmd = TEST_CMD_BASE + " # CONCURRENT_OK"
    proc, tsv, cap = _run_hook(
        cmd,
        tmp_path=tmp_path,
        ssh_env={"SSH_STUB_COMPUTE_APPS": "1234, ray::worker, 12000 MiB"},
    )
    assert proc.returncode == 0, proc.stderr
    body = _curl_body(cap)
    assert body["event"] == "hook_bypassed"
    assert body["payload"]["outcome"] == "concurrent_ok"
    assert body["payload"]["gpu_state"] is not None
    assert "ray::worker" in body["payload"]["gpu_state"]


def test_hook_forward_vram_ok(tmp_path):
    cmd = TEST_CMD_BASE + " # VRAM=8GB"
    proc, tsv, cap = _run_hook(
        cmd,
        tmp_path=tmp_path,
        ssh_env={"SSH_STUB_MEMORY": "24576, 31000"},
    )
    assert proc.returncode == 0, proc.stderr
    body = _curl_body(cap)
    assert body["event"] == "hook_bypassed"
    assert body["payload"]["outcome"] == "vram_ok"


def test_hook_forward_vram_saturated(tmp_path):
    cmd = TEST_CMD_BASE + " # VRAM=24GB"
    proc, tsv, cap = _run_hook(
        cmd,
        tmp_path=tmp_path,
        ssh_env={
            "SSH_STUB_MEMORY": "18432, 31000",
            "SSH_STUB_COMPUTE_APPS": "9999, holder, 12000 MiB",
        },
    )
    assert proc.returncode == 2, proc.stderr
    body = _curl_body(cap)
    assert body["event"] == "hook_blocked"
    assert body["payload"]["outcome"] == "vram_saturated"


def test_hook_forward_vram_probe_failed(tmp_path):
    cmd = TEST_CMD_BASE + " # VRAM=8GB"
    proc, tsv, cap = _run_hook(
        cmd,
        tmp_path=tmp_path,
        ssh_env={"SSH_STUB_MEMORY": "", "SSH_STUB_MEMORY_EXIT": "1"},
    )
    assert proc.returncode == 2, proc.stderr
    body = _curl_body(cap)
    assert body["event"] == "hook_blocked"
    assert body["payload"]["outcome"] == "vram_probe_failed"


def test_hook_forward_skipped_when_jobd_url_unset(tmp_path):
    proc, tsv, cap = _run_hook(TEST_CMD_BASE, tmp_path=tmp_path, jobd_url=None)
    assert proc.returncode == 2, proc.stderr
    assert not (cap / "curl_body").exists(), "curl should not be invoked when JOBD_URL is unset"
    assert "\tBLOCK\t" in tsv.read_text(), "TSV row must still be written"
