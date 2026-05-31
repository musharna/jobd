"""Sentinel: existing CLI/broker test suite must remain green at BASELINE count.

Run before any MCP-2 work. Re-run before tagging mcp-v1.
"""

import subprocess
import sys


def test_full_suite_green():
    # Invoke via the current interpreter (`sys.executable -m pytest`) so the
    # subprocess uses THIS venv. A bare `pytest` resolves to whatever is first
    # on PATH (e.g. a conda base env without fastapi), producing spurious
    # collection errors for any contributor whose PATH pytest differs.
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--ignore=tests/mcp", "-q"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"existing suite regressed:\n{result.stdout}\n{result.stderr}"
