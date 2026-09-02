"""The tracked tree carries no private paths, hosts, addresses or session trailers.

Written after the 2026-08-19 scrub was undone twelve days later by ordinary
feature work: a scrub is a state, not a commit, and nothing was checking the
state. Patterns are assembled from fragments so this file passes its own scan.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN = [
    "/home/" + "mjarnold",
    "/Users/" + "a2b32",
    "100.113." + "204.41",
    "100.91." + "179.102",
    "tail" + "86d19d",
    "mjarnold" + "gt76",
    "Claude-" + "Session:",
    "noreply@" + "anthropic.com",
]


def scan(paths: list[Path]) -> list[str]:
    hits: list[str] = []
    needles = [n.encode() for n in FORBIDDEN]
    for p in paths:
        try:
            data = p.read_bytes()
        except (IsADirectoryError, FileNotFoundError):
            continue
        for i, line in enumerate(data.split(b"\n"), 1):
            for n in needles:
                if n in line:
                    hits.append(
                        f"{p.relative_to(ROOT) if p.is_relative_to(ROOT) else p}:{i}: {n.decode()}"
                    )
    return hits


def _tracked() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout
    return [ROOT / f.decode() for f in out.split(b"\0") if f]


def test_tracked_tree_has_no_private_paths():
    files = _tracked()
    assert Path(__file__) in files, "this guard must itself be tracked"
    hits = scan(files)
    assert hits == [], "private content is tracked:\n" + "\n".join(hits)


def test_scanner_reports_every_planted_hit(tmp_path):
    """Positive control: the scanner is worth exactly what it can catch."""
    planted = tmp_path / "planted.bin"
    planted.write_bytes(b"\x89PNG\n" + b"\n".join(n.encode() for n in FORBIDDEN) + b"\n")
    hits = scan([planted])
    found = {h.split(": ", 1)[1] for h in hits}
    assert found == set(FORBIDDEN), sorted(set(FORBIDDEN) - found)
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
