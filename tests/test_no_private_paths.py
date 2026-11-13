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
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_ENV = "JOBD_PRIVACY_FORBIDDEN"
_FILE = Path.home() / ".config/jobd/privacy-forbidden.txt"
_raw = os.environ.get(_ENV) or (_FILE.read_text(encoding="utf-8") if _FILE.is_file() else "")
FORBIDDEN = [line.strip() for line in _raw.splitlines() if line.strip()]


# Set by ci.yml to "dependabot" only on a Dependabot-triggered pull_request run.
_NO_SECRET_ENV = "JOBD_PRIVACY_NO_SECRET"


def _dependabot_run_without_secrets() -> bool:
    """True only on a run the workflow marked AND GitHub says is Dependabot's
    pull_request. GITHUB_ACTOR and GITHUB_EVENT_NAME are set by the runner and
    a workflow cannot override them, so the marker alone cannot open the skip."""
    return (
        os.environ.get(_NO_SECRET_ENV) == "dependabot"
        and os.environ.get("GITHUB_ACTOR") == "dependabot[bot]"
        and os.environ.get("GITHUB_EVENT_NAME") == "pull_request"
    )


def _require_list() -> None:
    if FORBIDDEN:
        return
    if _dependabot_run_without_secrets():
        pytest.skip(
            "no forbidden list on a Dependabot pull_request run: GitHub gives "
            "Dependabot runs no Actions secrets. The scan runs on the push to main "
            "after merge; to scan here as well, add PRIVACY_FORBIDDEN as a "
            "Dependabot secret."
        )
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


# --- Dependabot runs (audit 2026-11-10 J-M1) ----------------------------------
# GitHub gives Dependabot-triggered runs no Actions secrets, so the list above is
# empty there and both scan tests failed every security-update PR. The workflow
# marks such a run explicitly; the scans skip, with a reason, only when the list
# is missing AND the run is marked AND GitHub itself says the actor is
# Dependabot on a pull_request. Anywhere else a missing list still fails.

_DEPENDABOT_ENV = {
    "JOBD_PRIVACY_NO_SECRET": "dependabot",
    "GITHUB_ACTOR": "dependabot[bot]",
    "GITHUB_EVENT_NAME": "pull_request",
}


@pytest.mark.parametrize(
    "drop,override",
    [
        ("JOBD_PRIVACY_NO_SECRET", None),  # not marked by the workflow
        (None, ("GITHUB_ACTOR", "musharna")),  # the owner's PR
        (None, ("GITHUB_EVENT_NAME", "push")),  # a push to main
        (None, ("JOBD_PRIVACY_NO_SECRET", "1")),  # marked with anything but the agreed value
    ],
)
def test_a_missing_list_still_fails_outside_a_dependabot_run(monkeypatch, drop, override):
    env = dict(_DEPENDABOT_ENV)
    if drop:
        env.pop(drop)
    if override:
        env[override[0]] = override[1]
    for k in _DEPENDABOT_ENV:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setitem(globals(), "FORBIDDEN", [])
    # Catch skip too: a skip is not an exception pytest.raises(Failed) sees,
    # so a wrongly opened skip would report this test as SKIPPED, not failed.
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception)) as outcome:
        _require_list()
    assert outcome.type is pytest.fail.Exception, f"skipped where it must fail: {outcome.value}"
    assert "no forbidden list" in str(outcome.value)


def test_a_marked_dependabot_run_skips_only_when_the_list_is_missing(monkeypatch):
    for k, v in _DEPENDABOT_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setitem(globals(), "FORBIDDEN", [])
    with pytest.raises(pytest.skip.Exception, match="Dependabot"):
        _require_list()
    # Positive control: with a list present, a marked run still scans.
    monkeypatch.setitem(globals(), "FORBIDDEN", ["x"])
    _require_list()


def test_the_real_scan_tests_skip_on_a_marked_run_and_fail_on_an_unmarked_one(tmp_path):
    """The two cases above, through a real pytest process with no list at all
    (HOME points at an empty directory, so the fallback file is absent)."""
    node = f"{Path(__file__).relative_to(ROOT)}::test_tracked_tree_has_no_private_paths"
    base = {k: v for k, v in os.environ.items() if k not in _DEPENDABOT_ENV and k != _ENV}
    base["HOME"] = str(tmp_path)

    def run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "pytest", node, "-q", "-rs", "-p", "no:cacheprovider"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    marked = run({**base, **_DEPENDABOT_ENV})
    assert marked.returncode == 0 and "1 skipped" in marked.stdout, marked.stdout
    assert "Dependabot" in marked.stdout, marked.stdout  # the reason is printed
    unmarked = run({**base, "GITHUB_ACTOR": "musharna", "GITHUB_EVENT_NAME": "pull_request"})
    assert unmarked.returncode == 1 and "1 failed" in unmarked.stdout, unmarked.stdout
