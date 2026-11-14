"""The tracked tree carries no private paths, hosts, addresses or session trailers.

Written after the 2026-08-19 scrub was undone twelve days later by ordinary
feature work: a scrub is a state, not a commit, and nothing was checking the
state.

The identifiers are not in the repository -- spelling them out, even in
fragments, would publish them. They are read, one per line, from the
environment variable JOBD_PRIVACY_FORBIDDEN (CI sets it from the
PRIVACY_FORBIDDEN repository secret) or else from
~/.config/jobd/privacy-forbidden.txt. With neither, both scan tests FAIL rather
than pass on an empty list (two narrow, tested exceptions are at the bottom:
Dependabot pull requests and the template-owned guardrails workflows). Hits are reported by entry number (#k), never by
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


# The workflows repo-template owns. They are the same file in every repo, so
# they cannot name this repo's secret; ci.yml's test jobs carry the scan.
_TEMPLATE_WORKFLOWS = ("guardrails", "nightly-guardrails")
# Where scripts/guardrails-setup.sh leaves that workflow's name for the step
# that re-runs the suite through sudo (GITHUB_* is dropped there, HOME is kept).
_NO_LIST_MARKER = Path.home() / ".config/jobd/privacy-no-list"


def _template_workflow_run() -> str | None:
    """The workflow's name on a GitHub run of a template-owned workflow, else
    None. GITHUB_ACTIONS and GITHUB_WORKFLOW are set by the runner and a
    workflow cannot override them; the marker file is written from those two
    by scripts/guardrails-setup.sh and counts only when it names one of them."""
    name = os.environ.get("GITHUB_WORKFLOW")
    if os.environ.get("GITHUB_ACTIONS") == "true" and name in _TEMPLATE_WORKFLOWS:
        return name
    if _NO_LIST_MARKER.is_file():
        marked = _NO_LIST_MARKER.read_text(encoding="utf-8").strip()
        if marked in _TEMPLATE_WORKFLOWS:
            return marked
    return None


def _require_list() -> None:
    if FORBIDDEN:
        return
    workflow = _template_workflow_run()
    if workflow:
        pytest.skip(
            f"no forbidden list in the template-owned '{workflow}' workflow: it is "
            "the same file in every repo and cannot name this repo's secret. "
            "ci.yml's test jobs scan every pull request and every push to main."
        )
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
# What the runner sets on a run of a template-owned workflow (see below).
_TEMPLATE_RUN_ENV = {"GITHUB_ACTIONS": "true", "GITHUB_WORKFLOW": "guardrails"}


def _env_without_the_list(home: Path) -> dict[str, str]:
    """This process's environment with no list and no run markers; HOME points
    at an empty directory, so the fallback file is absent too."""
    drop = {*_DEPENDABOT_ENV, *_TEMPLATE_RUN_ENV, _ENV}
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["HOME"] = str(home)
    return env


@pytest.mark.parametrize(
    "drop,override",
    [
        ("JOBD_PRIVACY_NO_SECRET", None),  # not marked by the workflow
        (None, ("GITHUB_ACTOR", "musharna")),  # the owner's PR
        (None, ("GITHUB_EVENT_NAME", "push")),  # a push to main
        (None, ("JOBD_PRIVACY_NO_SECRET", "1")),  # marked with anything but the agreed value
    ],
)
def test_a_missing_list_still_fails_outside_a_dependabot_run(monkeypatch, tmp_path, drop, override):
    monkeypatch.setitem(globals(), "_NO_LIST_MARKER", tmp_path / "absent")
    env = dict(_DEPENDABOT_ENV)
    if drop:
        env.pop(drop)
    if override:
        env[override[0]] = override[1]
    for k in (*_DEPENDABOT_ENV, *_TEMPLATE_RUN_ENV):
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


def test_a_marked_dependabot_run_skips_only_when_the_list_is_missing(monkeypatch, tmp_path):
    monkeypatch.setitem(globals(), "_NO_LIST_MARKER", tmp_path / "absent")
    for k in _TEMPLATE_RUN_ENV:
        monkeypatch.delenv(k, raising=False)
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
    base = _env_without_the_list(tmp_path)

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


# --- Template-owned workflows (guardrails adoption, 2026-11-14) ----------------
# guardrails.yml and nightly-guardrails.yml come from repo-template and are the
# same file in every repo, so they cannot name this repo's PRIVACY_FORBIDDEN
# secret: the list is empty on their runs. The scans skip there, with a reason,
# only when the list is missing AND the runner itself says the run is one of
# those two workflows (GITHUB_ACTIONS and GITHUB_WORKFLOW are set by the runner;
# a workflow cannot override them). ci.yml's test jobs still scan every pull
# request and every push to main, and a missing list still fails there.


@pytest.mark.parametrize("workflow", ["guardrails", "nightly-guardrails"])
def test_a_template_workflow_run_skips_only_when_the_list_is_missing(
    monkeypatch, tmp_path, workflow
):
    monkeypatch.setitem(globals(), "_NO_LIST_MARKER", tmp_path / "absent")
    for k in _DEPENDABOT_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_WORKFLOW", workflow)
    monkeypatch.setitem(globals(), "FORBIDDEN", [])
    with pytest.raises(pytest.skip.Exception, match=f"template-owned '{workflow}' workflow"):
        _require_list()
    # Positive control: with a list present, the same run still scans.
    monkeypatch.setitem(globals(), "FORBIDDEN", ["x"])
    _require_list()


@pytest.mark.parametrize(
    "env",
    [
        {"GITHUB_ACTIONS": "true", "GITHUB_WORKFLOW": "CI"},  # this repo's own workflow
        {"GITHUB_ACTIONS": "true", "GITHUB_WORKFLOW": "release"},
        {"GITHUB_ACTIONS": "true", "GITHUB_WORKFLOW": "Guardrails"},  # not the name
        {"GITHUB_ACTIONS": "true", "GITHUB_WORKFLOW": "guardrails-lite"},
        {"GITHUB_ACTIONS": "true"},  # no workflow name
        {"GITHUB_WORKFLOW": "guardrails"},  # a shell with a stray variable, not a runner
    ],
)
def test_a_missing_list_still_fails_outside_a_template_workflow_run(monkeypatch, tmp_path, env):
    monkeypatch.setitem(globals(), "_NO_LIST_MARKER", tmp_path / "absent")
    for k in (*_DEPENDABOT_ENV, *_TEMPLATE_RUN_ENV):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setitem(globals(), "FORBIDDEN", [])
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception)) as outcome:
        _require_list()
    assert outcome.type is pytest.fail.Exception, f"skipped where it must fail: {outcome.value}"
    assert "no forbidden list" in str(outcome.value)


def test_the_real_scan_tests_skip_in_a_template_workflow_and_fail_in_ci(tmp_path):
    """The two cases above, through a real pytest process with no list at all."""
    node = f"{Path(__file__).relative_to(ROOT)}::test_tracked_tree_has_no_private_paths"
    base = _env_without_the_list(tmp_path)

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

    nightly = run({**base, "GITHUB_ACTIONS": "true", "GITHUB_WORKFLOW": "nightly-guardrails"})
    assert nightly.returncode == 0 and "1 skipped" in nightly.stdout, nightly.stdout
    assert "template-owned" in nightly.stdout, nightly.stdout  # the reason is printed
    ci = run({**base, "GITHUB_ACTIONS": "true", "GITHUB_WORKFLOW": "CI"})
    assert ci.returncode == 1 and "1 failed" in ci.stdout, ci.stdout


# --- The same fact across the sudo hop ----------------------------------------
# guardrails.yml re-runs the suite with no network through sudo, which keeps a
# short list of variables (KEEP=... in the workflow) and drops GITHUB_*. On the
# adoption PR's first CI run the skip above held in the plain test step and
# both scans failed in the no-network one. scripts/guardrails-setup.sh, which
# the template workflows run first, now leaves the workflow's name in a file
# under HOME (HOME is kept), and the scans accept that file as well.


@pytest.mark.parametrize("workflow", ["guardrails", "nightly-guardrails"])
def test_the_marker_file_skips_only_when_the_list_is_missing(monkeypatch, tmp_path, workflow):
    for k in (*_DEPENDABOT_ENV, *_TEMPLATE_RUN_ENV):
        monkeypatch.delenv(k, raising=False)
    marker = tmp_path / "privacy-no-list"
    marker.write_text(workflow + "\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "_NO_LIST_MARKER", marker)
    monkeypatch.setitem(globals(), "FORBIDDEN", [])
    with pytest.raises(pytest.skip.Exception, match=f"template-owned '{workflow}' workflow"):
        _require_list()
    # Positive control: with a list present, the same run still scans.
    monkeypatch.setitem(globals(), "FORBIDDEN", ["x"])
    _require_list()


@pytest.mark.parametrize("content", ["", "CI\n", "Guardrails\n", "guardrails-lite\n", "1\n"])
def test_a_marker_file_naming_anything_else_still_fails(monkeypatch, tmp_path, content):
    for k in (*_DEPENDABOT_ENV, *_TEMPLATE_RUN_ENV):
        monkeypatch.delenv(k, raising=False)
    marker = tmp_path / "privacy-no-list"
    marker.write_text(content, encoding="utf-8")
    monkeypatch.setitem(globals(), "_NO_LIST_MARKER", marker)
    monkeypatch.setitem(globals(), "FORBIDDEN", [])
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception)) as outcome:
        _require_list()
    assert outcome.type is pytest.fail.Exception, f"skipped where it must fail: {outcome.value}"


def _kept_by_the_no_network_step() -> list[str]:
    """The variable names guardrails.yml carries across its sudo hop, read from
    the workflow itself so this test runs under the step's real environment."""
    text = (ROOT / ".github/workflows/guardrails.yml").read_text(encoding="utf-8")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("KEEP=")]
    assert len(lines) == 1, lines
    kept = lines[0].removeprefix("KEEP=").split(",")
    assert "HOME" in kept and not any(k.startswith("GITHUB_") for k in kept), kept
    return kept


def test_the_setup_script_carries_the_skip_across_the_sudo_hop(tmp_path):
    """The real script, then the real scan test in a real pytest process whose
    environment holds only what the no-network step keeps."""
    node = f"{Path(__file__).relative_to(ROOT)}::test_tracked_tree_has_no_private_paths"
    script = ROOT / "scripts/guardrails-setup.sh"
    kept = _kept_by_the_no_network_step()

    def scan_after_setup(home: Path, workflow: str) -> subprocess.CompletedProcess[str]:
        home.mkdir()
        runner = {
            **_env_without_the_list(home),
            "GITHUB_ACTIONS": "true",
            "GITHUB_WORKFLOW": workflow,
        }
        subprocess.run(["bash", str(script)], cwd=ROOT, env=runner, check=True, timeout=60)
        across = {k: v for k, v in runner.items() if k in kept}
        assert "GITHUB_WORKFLOW" not in across and across["HOME"] == str(home)
        return subprocess.run(
            [sys.executable, "-m", "pytest", node, "-q", "-rs", "-p", "no:cacheprovider"],
            cwd=ROOT,
            env=across,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    template = scan_after_setup(tmp_path / "template", "guardrails")
    assert template.returncode == 0 and "1 skipped" in template.stdout, template.stdout
    assert "template-owned 'guardrails' workflow" in template.stdout, template.stdout
    # The same script on this repo's own workflow leaves nothing, and the scan fails.
    ci = scan_after_setup(tmp_path / "ci", "CI")
    assert not (tmp_path / "ci/.config/jobd/privacy-no-list").exists()
    assert ci.returncode == 1 and "1 failed" in ci.stdout, ci.stdout
    # Nor does a shell that merely has the variable set, with no runner behind it.
    stray = tmp_path / "stray"
    stray.mkdir()
    shell = {**_env_without_the_list(stray), "GITHUB_WORKFLOW": "guardrails"}
    subprocess.run(["bash", str(script)], cwd=ROOT, env=shell, check=True, timeout=60)
    assert not (stray / ".config/jobd/privacy-no-list").exists()
