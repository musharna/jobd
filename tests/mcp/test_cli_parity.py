"""Sentinel: existing CLI/broker test suite must remain green at BASELINE count.

Run before any MCP-2 work. Re-run before tagging mcp-v1.
"""

import subprocess


def test_full_suite_green():
    result = subprocess.run(
        ["pytest", "--ignore=tests/mcp", "-q"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"existing suite regressed:\n{result.stdout}\n{result.stderr}"
