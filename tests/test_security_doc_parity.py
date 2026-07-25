"""Every doc that describes the broker's auth model must describe the broker
that actually exists.

The 2026-07-25 security audit found docs/security.md claiming "every request
needs `Authorization: Bearer`" while THREE endpoints were exempt from that wall
AND from the source-IP ACL — one of which (`/metrics`) publishes every worker's
hostname and version. It also still advertised `jobd_job_get`, an MCP tool
deliberately removed on 2026-07-12.

Neither drift was anyone's mistake in particular: nothing pinned this file.
README and the agent cookbook got a parity test in #76; the page an operator
reads to decide whether it is safe to expose the broker did not. This is that
test.

The pre-launch audit later the same day found the SAME overclaim one file over:
README said "Two stacked controls" and stopped there. The first version of this
test pinned security.md ONLY, so fixing the page I happened to be looking at
left the page far more people actually read still wrong. **That is why the
README assertions below exist: a guard scoped to one of two documents saying
the same thing is what lets them drift apart.**

Everything derives from the live constants in both directions, so it fails when
a path is added to the code but not the docs, AND when a doc lists one the code
no longer exempts.
"""

from __future__ import annotations

import re
from pathlib import Path

from jobd.auth import _ACL_EXEMPT_ROOT, _UNAUTHENTICATED_PATHS
from jobd.mcp.server import _TOOLS

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SECURITY_MD = _REPO_ROOT / "docs" / "security.md"
_README_MD = _REPO_ROOT / "README.md"
_SECTION_HEADING = "## Unauthenticated surface"
_README_HEADING = "## Security"


def _security_text() -> str:
    return _SECURITY_MD.read_text()


def _section(text: str, heading: str, *, where: str, why: str) -> str:
    assert heading in text, f"{where} lost its {heading!r} section. {why}"
    after = text.split(heading, 1)[1]
    # Up to the next top-level section.
    return after.split("\n## ", 1)[0]


def _unauthenticated_section() -> str:
    return _section(
        _security_text(),
        _SECTION_HEADING,
        where="docs/security.md",
        why=(
            "That section is the only place an operator is told which endpoints answer "
            "without a token or a source-IP check — including that /metrics publishes "
            "worker hostnames and versions. Restore it rather than deleting this test."
        ),
    )


def _readme_security_section() -> str:
    return _section(
        _README_MD.read_text(),
        _README_HEADING,
        where="README.md",
        why=(
            "The README security section is what a drive-by reader judges the project's "
            "security posture by. Restore it rather than deleting this test."
        ),
    )


def test_every_doubly_exempt_endpoint_is_documented():
    """Code → doc. A new exemption must be disclosed, not just implemented."""
    section = _unauthenticated_section()
    expected = set(_UNAUTHENTICATED_PATHS) | {_ACL_EXEMPT_ROOT}
    missing = sorted(p for p in expected if f"`{p}`" not in section)
    assert not missing, (
        f"these endpoints answer without a bearer token and without the tailnet ACL, "
        f"but are not listed in docs/security.md's '{_SECTION_HEADING}' section: "
        f"{missing}. Widening the unauthenticated surface without telling the operator "
        "is how a port-forward becomes a fleet inventory leak."
    )


def test_the_doc_does_not_invent_exemptions_that_do_not_exist():
    """Doc → code. A stale entry is as misleading as a missing one."""
    section = _unauthenticated_section()
    real = set(_UNAUTHENTICATED_PATHS) | {_ACL_EXEMPT_ROOT}
    # Only inspect the table rows, so prose mentioning /health doesn't trip this.
    claimed = {
        m.group(1)
        for line in section.splitlines()
        if line.strip().startswith("| `")
        for m in [re.match(r"\|\s*`([^`]+)`", line.strip())]
        if m
    }
    stale = sorted(claimed - real)
    assert not stale, (
        f"docs/security.md lists {stale} as unauthenticated, but the code does not "
        "exempt them. A security page that overstates the hole trains operators to "
        "ignore it."
    )


def test_the_bearer_claim_is_qualified():
    """The specific sentence the audit caught. It said every request needs a
    token, full stop, which was false."""
    text = _security_text()
    for match in re.finditer(r"every request needs[^\n.]*", text):
        sentence = match.group(0)
        assert "except" in sentence.lower(), (
            "docs/security.md states 'every request needs <token>' without qualifying "
            f"it: {sentence!r}. Three endpoints are exempt from both walls; saying "
            "otherwise is the exact claim the 2026-07-25 audit flagged."
        )


def test_readme_discloses_every_doubly_exempt_endpoint():
    """Code → README. The pre-launch audit's PL-2 finding, pinned.

    Same derivation as the security.md guard above, against the document a
    stranger actually reads. A new exemption must be disclosed in BOTH.
    """
    section = _readme_security_section()
    expected = set(_UNAUTHENTICATED_PATHS) | {_ACL_EXEMPT_ROOT}
    missing = sorted(p for p in expected if f"`{p}`" not in section)
    assert not missing, (
        f"README's '{_README_HEADING}' section enumerates the broker's auth controls "
        f"but does not disclose that these endpoints answer without either of them: "
        f"{missing}. docs/security.md documents them; a README that stops at the walls "
        "overclaims to the one audience least likely to read further."
    )


def test_readme_does_not_present_the_controls_as_total():
    """The specific PL-2 sentence: 'Two stacked controls:' with no caveat.

    Naming the paths is not enough on its own — they could appear in prose that
    still implies full coverage. Require the exemption to be stated as such.
    """
    section = _readme_security_section()
    assert "exempt" in section.lower(), (
        f"README's '{_README_HEADING}' section describes the broker's controls without "
        "the word 'exempt' anywhere. Three endpoints answer with neither wall, and "
        "/metrics publishes every worker's hostname and version — a reader who takes "
        "the controls as total and then curls /metrics has been misled."
    )


def test_security_md_only_names_mcp_tools_that_exist():
    """Same guard #76 gave README and the cookbook. `jobd_job_get` was removed
    on 2026-07-12 and lingered here for two weeks."""
    real = {name for name, *_ in _TOOLS}  # same unpack as tests/mcp/test_readme_tool_parity.py
    mentioned = set(re.findall(r"`(jobd_[a-z_]+)`", _security_text()))
    ghosts = sorted(mentioned - real)
    assert not ghosts, (
        f"docs/security.md references MCP tools that do not exist: {ghosts}. "
        f"Real tools: {sorted(real)}."
    )
