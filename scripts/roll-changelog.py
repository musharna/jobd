#!/usr/bin/env python3
"""Roll changelog.d/ fragments into a released section of CHANGELOG.md.

Why fragments: every PR used to insert at the top of ``[Unreleased]``, so any
two PRs in flight conflicted on the same lines at every merge (the v0.5.26
merge train hit this five PRs in a row). Now each PR adds its own file under
``changelog.d/`` — no shared lines, no conflicts — and this script assembles
them at release time.

Fragment contract (enforced here and by tests/test_changelog_fragments.py):
  - filename ``<slug>.<category>.md`` with category one of CATEGORIES
  - content is one or more markdown bullets; the first line starts with "- "
  - ``README.md`` is documentation, not a fragment

Usage:
    python3 scripts/roll-changelog.py 0.5.27 [--date 2026-07-15] [--repo-root .]

Writes ``## [<version>] — <date>`` directly under ``## [Unreleased]`` with the
categories in canonical order, then deletes the consumed fragments. Refuses to
run (exit 2, CHANGELOG untouched) on: no fragments, a bad version string, a
version already present, an unknown category, a malformed fragment, or a stray
non-fragment file in changelog.d/.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import re
import sys
from pathlib import Path
from typing import NoReturn

# Canonical section order — matches the hand-rolled sections of past releases.
CATEGORIES = ("fixed", "security", "changed", "added", "deprecated", "removed")

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UNRELEASED = "## [Unreleased]"


def die(msg: str) -> NoReturn:
    print(f"roll-changelog: {msg}", file=sys.stderr)
    raise SystemExit(2)


def collect_fragments(frag_dir: Path) -> dict[str, list[tuple[str, str]]]:
    """Return {category: [(filename, content), ...]} sorted by filename."""
    if not frag_dir.is_dir():
        die(f"{frag_dir} does not exist")
    by_cat: dict[str, list[tuple[str, str]]] = {c: [] for c in CATEGORIES}
    for path in sorted(frag_dir.iterdir()):
        if path.name == "README.md":
            continue
        parts = path.name.split(".")
        if len(parts) != 3 or parts[2] != "md":
            die(f"{path.name}: not <slug>.<category>.md")
        category = parts[1]
        if category not in CATEGORIES:
            die(f"{path.name}: unknown category {category!r} (want one of {', '.join(CATEGORIES)})")
        content = path.read_text().strip()
        if not content.startswith("- "):
            die(f"{path.name}: fragment must start with a markdown bullet ('- ')")
        by_cat[category].append((path.name, content))
    if not any(by_cat.values()):
        die(f"no fragments in {frag_dir} — nothing to release")
    return by_cat


def build_section(version: str, date: str, by_cat: dict[str, list[tuple[str, str]]]) -> str:
    lines = [f"## [{version}] — {date}", ""]
    for category in CATEGORIES:
        entries = by_cat[category]
        if not entries:
            continue
        lines.append(f"### {category.capitalize()}")
        lines.append("")
        for _name, content in entries:
            lines.append(content)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="release version, e.g. 0.5.27")
    parser.add_argument("--date", default=None, help="release date YYYY-MM-DD (default: today)")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root holding CHANGELOG.md and changelog.d/",
    )
    args = parser.parse_args(argv)

    if not _VERSION_RE.match(args.version):
        die(f"version {args.version!r} is not plain X.Y.Z")
    date = args.date or _dt.date.today().isoformat()
    if not _DATE_RE.match(date):
        die(f"date {date!r} is not YYYY-MM-DD")

    changelog = args.repo_root / "CHANGELOG.md"
    frag_dir = args.repo_root / "changelog.d"
    if not changelog.is_file():
        die(f"{changelog} does not exist")
    text = changelog.read_text()
    if _UNRELEASED not in text:
        die(f"CHANGELOG.md has no '{_UNRELEASED}' heading")
    if f"## [{args.version}]" in text:
        die(f"version {args.version} already has a section in CHANGELOG.md")

    by_cat = collect_fragments(frag_dir)
    section = build_section(args.version, date, by_cat)

    head, _sep, tail = text.partition(_UNRELEASED)
    if not tail.lstrip().startswith("## ["):
        # There is stray content under [Unreleased]; refuse rather than eat it.
        die("the [Unreleased] section is not empty — move its entries into changelog.d/ first")
    # Keep [Unreleased] as a standing (empty) heading above the new section.
    changelog.write_text(f"{head}{_UNRELEASED}\n\n{section}{tail.lstrip()}")

    consumed = [name for entries in by_cat.values() for name, _ in entries]
    for name in consumed:
        (frag_dir / name).unlink()
    print(f"rolled {len(consumed)} fragment(s) into [{args.version}] — {date}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
