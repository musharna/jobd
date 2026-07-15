"""The changelog-fragments contract (see changelog.d/README.md).

Two halves:
  1. Standing repo assertions — [Unreleased] stays empty (entries go in
     changelog.d/), and every committed fragment obeys the naming/bullet
     contract. These are what actually kill the merge-train conflicts: the
     moment someone pastes an entry under [Unreleased] the old way, CI points
     them at changelog.d/.
  2. Real-EXECUTION tests of scripts/roll-changelog.py against fixture repos —
     the deploy-lint lesson applies here too: a release helper that is only
     ever text-read will fail for real at the one moment it runs, mid-release.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "roll-changelog.py"
_CHANGELOG = _REPO_ROOT / "CHANGELOG.md"
_FRAG_DIR = _REPO_ROOT / "changelog.d"

_CATEGORIES = ("fixed", "security", "changed", "added", "deprecated", "removed")


# --- standing repo assertions ------------------------------------------------


def test_unreleased_section_is_empty():
    """All pending entries live in changelog.d/, never under [Unreleased]."""
    text = _CHANGELOG.read_text()
    match = re.search(r"^## \[Unreleased\]\n(.*?)^## \[", text, re.S | re.M)
    assert match, "CHANGELOG.md must keep an [Unreleased] heading above the releases"
    stray = match.group(1).strip()
    assert not stray, (
        "Entries under [Unreleased] conflict with every other in-flight PR — "
        f"move them to changelog.d/<slug>.<category>.md:\n{stray}"
    )


def test_committed_fragments_obey_contract():
    assert _FRAG_DIR.is_dir()
    assert (_FRAG_DIR / "README.md").is_file(), "changelog.d/README.md documents the scheme"
    for path in _FRAG_DIR.iterdir():
        if path.name == "README.md":
            continue
        parts = path.name.split(".")
        assert len(parts) == 3 and parts[2] == "md", f"{path.name}: want <slug>.<category>.md"
        assert parts[1] in _CATEGORIES, f"{path.name}: unknown category {parts[1]!r}"
        assert path.read_text().strip().startswith("- "), (
            f"{path.name}: fragment must start with a markdown bullet"
        )


def test_contributor_docs_point_at_fragments():
    """The PR template and CONTRIBUTING must send authors to changelog.d/,
    not back to editing CHANGELOG.md directly."""
    template = (_REPO_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text()
    contributing = (_REPO_ROOT / "CONTRIBUTING.md").read_text()
    assert "changelog.d/" in template
    assert "changelog.d/" in contributing


# --- real execution of roll-changelog.py -------------------------------------


def _roll(repo: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *argv, "--repo-root", str(repo)],
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n"
        "\n"
        "All notable changes.\n"
        "\n"
        "## [Unreleased]\n"
        "\n"
        "## [0.5.26] — 2026-07-15\n"
        "\n"
        "### Fixed\n"
        "\n"
        "- old entry\n"
    )
    frag = tmp_path / "changelog.d"
    frag.mkdir()
    (frag / "README.md").write_text("docs, not a fragment\n")
    return tmp_path


def test_roll_happy_path(repo: Path):
    frag = repo / "changelog.d"
    (frag / "b-second.fixed.md").write_text("- **Second fix.** Details.\n")
    (frag / "a-first.fixed.md").write_text("- **First fix.** Details.\n")
    (frag / "harden.security.md").write_text("- **Hardening.** Details.\n")
    (frag / "feature.added.md").write_text(
        "- **A feature.** With a continuation paragraph:\n\n  indented detail line.\n"
    )

    result = _roll(repo, "0.5.27", "--date", "2026-07-16")
    assert result.returncode == 0, result.stderr

    text = (repo / "CHANGELOG.md").read_text()
    # New section sits directly under a still-present, still-empty [Unreleased].
    assert "## [Unreleased]\n\n## [0.5.27] — 2026-07-16\n" in text
    # Canonical category order, fragments filename-sorted within a category.
    assert text.index("### Fixed") < text.index("### Security") < text.index("### Added")
    assert text.index("**First fix.**") < text.index("**Second fix.**")
    assert "indented detail line." in text
    # Prior releases untouched, consumed fragments deleted, README kept.
    assert "## [0.5.26] — 2026-07-15" in text
    assert sorted(p.name for p in frag.iterdir()) == ["README.md"]


def test_roll_refuses_empty_fragment_dir(repo: Path):
    before = (repo / "CHANGELOG.md").read_text()
    result = _roll(repo, "0.5.27")
    assert result.returncode == 2
    assert "nothing to release" in result.stderr
    assert (repo / "CHANGELOG.md").read_text() == before


@pytest.mark.parametrize(
    ("filename", "content", "expect"),
    [
        ("oops.bugfix.md", "- entry\n", "unknown category"),
        ("no-category.md", "- entry\n", "not <slug>.<category>.md"),
        ("bad-bullet.fixed.md", "just prose, no bullet\n", "markdown bullet"),
    ],
)
def test_roll_refuses_bad_fragments(repo: Path, filename: str, content: str, expect: str):
    (repo / "changelog.d" / filename).write_text(content)
    before = (repo / "CHANGELOG.md").read_text()
    result = _roll(repo, "0.5.27")
    assert result.returncode == 2
    assert expect in result.stderr
    assert (repo / "CHANGELOG.md").read_text() == before
    assert (repo / "changelog.d" / filename).exists(), "refusal must not delete fragments"


def test_roll_refuses_duplicate_version_and_bad_version(repo: Path):
    (repo / "changelog.d" / "x.fixed.md").write_text("- entry\n")
    result = _roll(repo, "0.5.26")
    assert result.returncode == 2 and "already has a section" in result.stderr
    result = _roll(repo, "v0.5.27")
    assert result.returncode == 2 and "not plain X.Y.Z" in result.stderr


def test_roll_refuses_stray_unreleased_content(repo: Path):
    """Content pasted under [Unreleased] the old way must never be silently
    eaten by the roll — the script stops and says where it belongs."""
    (repo / "changelog.d" / "x.fixed.md").write_text("- entry\n")
    changelog = repo / "CHANGELOG.md"
    changelog.write_text(
        changelog.read_text().replace(
            "## [Unreleased]\n", "## [Unreleased]\n\n- pasted the old way\n"
        )
    )
    before = changelog.read_text()
    result = _roll(repo, "0.5.27")
    assert result.returncode == 2
    assert "move its entries into changelog.d/" in result.stderr
    assert changelog.read_text() == before
    assert (repo / "changelog.d" / "x.fixed.md").exists()
