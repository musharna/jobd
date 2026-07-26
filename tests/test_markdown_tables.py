"""Markdown tables must survive GitHub's renderer.

GitHub-flavoured Markdown splits a table row into cells on `|` **even when the
pipe sits inside a code span** — backticks do not protect it. A row written as

    | piped to `foo` | `... | job submit --stdin` — one job per line |

therefore renders as THREE cells in a two-column table, and the renderer drops
the overflow: the entire right-hand cell collapses to a dangling `` `... ``.

That is not hypothetical. README.md's pueue/task-spooler migration table shipped
exactly this bug and rendered broken on the repo's front page for weeks. It was
found during pre-launch checks by pushing the table through GitHub's own
/markdown API, not by reading the source — which is the point: the source looks
completely fine, and every local editor and previewer renders it fine too.

It was the worst possible row to lose. That table is the conversion pitch aimed
at pueue and tsp users, and the broken row was the one showing them how to move
their piped-batch workflow across.

This sweeps every tracked Markdown file rather than the one file that happened
to be wrong, because the lesson from `test_security_doc_parity.py` is that a
guard scoped to the document you were already looking at is what lets the others
drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".claude", "changelog.d"}

# A code span, i.e. text between single backticks.
_CODE_SPAN = re.compile(r"`([^`]*)`")


def _markdown_files() -> list[Path]:
    return sorted(
        p for p in _REPO_ROOT.rglob("*.md") if not _SKIP_DIRS & set(p.relative_to(_REPO_ROOT).parts)
    )


def _is_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 1


def _offending_spans(line: str) -> list[str]:
    """Code spans in a table row that contain a pipe the renderer will act on."""
    return [span for span in _CODE_SPAN.findall(line) if "|" in span.replace(r"\|", "")]


def test_markdown_files_are_discoverable() -> None:
    """Guard the guard: a bad skip list would make every assertion below vacuous."""
    files = _markdown_files()
    assert len(files) >= 5, f"expected to sweep several .md files, found {len(files)}"
    assert _REPO_ROOT / "README.md" in files, "README.md must be covered"


@pytest.mark.parametrize("path", _markdown_files(), ids=lambda p: str(p.name))
def test_no_unescaped_pipe_inside_table_code_spans(path: Path) -> None:
    failures = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not _is_table_row(line):
            continue
        for span in _offending_spans(line):
            failures.append(f"  {path.relative_to(_REPO_ROOT)}:{lineno}  `{span}`")

    assert not failures, (
        "Unescaped `|` inside a code span in a Markdown table row. GitHub will "
        "split the cell there and silently drop everything after it. Escape the "
        "pipe as `\\|`:\n" + "\n".join(failures)
    )


def test_guard_catches_the_original_readme_bug() -> None:
    """The exact row that shipped broken must be detected by the check above."""
    regressed = (
        "| commands piped to `simple_gpu_scheduler` "
        "| `... | job submit -p <project> --stdin` — one job per line, fleet-wide |"
    )
    assert _is_table_row(regressed)
    assert _offending_spans(regressed), "guard would not have caught the original bug"

    fixed = regressed.replace("`... | job", r"`... \| job")
    assert not _offending_spans(fixed), "guard must accept the escaped form"
