"""The tracked tree carries no private paths, hosts, addresses or session trailers.

Written after the 2026-08-19 scrub was undone twelve days later by ordinary
feature work: a scrub is a state, not a commit, and nothing was checking the
state.

The identifiers are not in the repository -- spelling them out, even in
fragments, would publish them. They are read, one per line, from the
environment variable JOBD_PRIVACY_FORBIDDEN (CI sets it from the
PRIVACY_FORBIDDEN repository secret) or else from
~/.config/jobd/privacy-forbidden.txt. With neither, both scan tests FAIL rather
than pass on an empty list. Hits are reported by entry number (#k), never by
the matched text, so a CI log cannot print what it found.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_ENV = "JOBD_PRIVACY_FORBIDDEN"
_FILE = Path.home() / ".config/jobd/privacy-forbidden.txt"
_raw = os.environ.get(_ENV) or (_FILE.read_text(encoding="utf-8") if _FILE.is_file() else "")
FORBIDDEN = [line.strip() for line in _raw.splitlines() if line.strip()]


def _require_list() -> None:
    if not FORBIDDEN:
        pytest.fail(f"no forbidden list: set {_ENV} or write {_FILE} (one identifier per line)")


def scan(paths: list[Path]) -> list[str]:
    hits: list[str] = []
    needles = [n.encode() for n in FORBIDDEN]
    for p in paths:
        try:
            data = p.read_bytes()
        except (IsADirectoryError, FileNotFoundError):
            continue
        for i, line in enumerate(data.split(b"\n"), 1):
            for k, n in enumerate(needles, 1):
                if n in line:
                    hits.append(f"{p.relative_to(ROOT) if p.is_relative_to(ROOT) else p}:{i}: #{k}")
    return hits


def _tracked() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout
    return [ROOT / f.decode() for f in out.split(b"\0") if f]


def test_tracked_tree_has_no_private_paths():
    _require_list()
    files = _tracked()
    assert Path(__file__) in files, "this guard must itself be tracked"
    hits = scan(files)
    assert hits == [], "private content is tracked:\n" + "\n".join(hits)


def test_scanner_reports_every_planted_hit(tmp_path):
    """Positive control: the scanner is worth exactly what it can catch."""
    _require_list()
    planted = tmp_path / "planted.bin"
    planted.write_bytes(b"\x89PNG\n" + b"\n".join(n.encode() for n in FORBIDDEN) + b"\n")
    hits = scan([planted])
    found = {h.split(": ", 1)[1] for h in hits}
    want = {f"#{k}" for k in range(1, len(FORBIDDEN) + 1)}
    assert found == want, sorted(want - found)
    assert all(h.split(":")[1].isdigit() for h in hits)


def test_the_private_overlay_is_gitignored():
    """The real roots and names live in config/projects.local.yaml. If git does
    not ignore that path, the deploy step that copies it beside projects.yaml
    turns the next `git add -A` into a publication of everything this file
    exists to keep private. (#129 appended the rule as one line containing
    literal backslash-n sequences, so the pattern never matched.)"""
    r = subprocess.run(
        ["git", "check-ignore", "-q", "config/projects.local.yaml"], cwd=ROOT, check=False
    )
    assert r.returncode == 0, "config/projects.local.yaml is not gitignored"
