"""docs/configuration.md is the complete JOBD_* catalog — enforced, not aspired.

~30 env vars were read ad-hoc across the codebase with no single place to
discover them. The fix is a documented catalog plus THIS drift test, in both
directions: a variable added to the source without a catalog row fails CI
(undocumented config is config nobody finds), and a catalog row whose variable
no longer exists anywhere fails CI (stale docs are worse than none — the
07-01 audit chased a `--env` CLI flag that had never existed).

Deliberately NOT a frozen settings object: most knobs are read at the point of
use (sweep passes re-read per tick), which keeps them runtime-tunable and
monkeypatch-able; centralizing the reads would change that behavior for zero
functional gain.
"""

from __future__ import annotations

import ast
import contextlib
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CATALOG = _REPO_ROOT / "docs" / "configuration.md"

_NAME_RE = re.compile(r"^JOBD_[A-Z0-9_]+$")


def _literals_under(root: Path) -> set[str]:
    """Every JOBD_* string literal in the Python sources under root. Literal
    sweep rather than call-pattern matching on purpose: reads go through
    helpers (`_env_int(...)`, `os.environ.get`, subscripts), and matching call
    shapes would silently miss the next helper someone writes."""
    names: set[str] = set()
    for f in root.rglob("*.py"):
        tree = ast.parse(f.read_text(), filename=str(f))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _NAME_RE.match(node.value)
            ):
                names.add(node.value)
    return names


def _documented() -> set[str]:
    """Variables with a catalog row — a table line whose first cell is the
    backticked name. Prose mentions don't count as documentation."""
    names: set[str] = set()
    for line in _CATALOG.read_text().splitlines():
        m = re.match(r"\|\s*`(JOBD_[A-Z0-9_]+)`\s*\|", line)
        if m:
            names.add(m.group(1))
    return names


def test_every_source_variable_has_a_catalog_row():
    undocumented = _literals_under(_REPO_ROOT / "src") - _documented()
    assert not undocumented, (
        f"JOBD_* variables in src/ without a row in docs/configuration.md: "
        f"{sorted(undocumented)} — add a row (Variable | Default | Purpose)."
    )


def test_every_catalog_row_still_exists_in_the_repo():
    # Reverse direction: docs may also cover test/CI gates, so the existence
    # check spans source, tests, workflows, and scripts.
    known = _literals_under(_REPO_ROOT / "src") | _literals_under(_REPO_ROOT / "tests")
    for extra in (".github", "scripts"):
        for f in (_REPO_ROOT / extra).rglob("*"):
            if f.is_file() and f.suffix in (".yml", ".yaml", ".sh", ".py", ""):
                with contextlib.suppress(UnicodeDecodeError, OSError):
                    known |= set(re.findall(r"JOBD_[A-Z0-9_]+", f.read_text()))
    stale = _documented() - known
    assert not stale, (
        f"docs/configuration.md documents variables that no longer exist anywhere: "
        f"{sorted(stale)} — delete the stale rows."
    )


def test_catalog_rows_are_complete_rows():
    """Each row needs all three cells filled — a name with an empty Purpose is
    a checkbox, not documentation."""
    for line in _CATALOG.read_text().splitlines():
        if re.match(r"\|\s*`JOBD_", line):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            assert all(cells), f"catalog row has an empty cell: {line!r}"
            assert len(cells) in (2, 3), f"unexpected column count: {line!r}"
