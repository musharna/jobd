"""The README's advertised MCP tool list must match the registered tools.

This drift already happened: the README advertised `jobd_job_get` (long gone)
and omitted `jobd_events` — the exact stale-hand-list class the schema parity
tests kill inside the MCP server, now applied to the front page. The
agent cookbook is held to the same standard.
"""

from __future__ import annotations

import re
from pathlib import Path

from jobd.mcp.server import _TOOLS

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_REGISTERED = {name for name, *_ in _TOOLS}


def _mentioned(path: Path) -> set[str]:
    return set(re.findall(r"jobd_[a-z_]+", path.read_text()))


def test_readme_advertises_exactly_the_registered_tools():
    mentioned = _mentioned(_REPO_ROOT / "README.md")
    assert mentioned >= _REGISTERED, (
        f"README omits registered MCP tools: {sorted(_REGISTERED - mentioned)}"
    )
    ghosts = {m for m in mentioned if m not in _REGISTERED}
    assert not ghosts, f"README advertises MCP tools that don't exist: {sorted(ghosts)}"


def test_cookbook_mentions_no_ghost_tools():
    ghosts = {
        m for m in _mentioned(_REPO_ROOT / "docs" / "agent-cookbook.md") if m not in _REGISTERED
    }
    assert not ghosts, f"agent-cookbook.md references MCP tools that don't exist: {sorted(ghosts)}"
