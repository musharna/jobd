"""`job projects set/nudge` driven against the REAL broker app.

Why this file exists. v0.5.40 taught the write path to fold a spelling onto the
registered project, and tests/test_api.py asserts that fold at the endpoint --
including, literally, `assert "PROJECT-B" not in body`. That assertion states
the crash precondition and stops one layer short of the only consumer: the CLI
went on indexing the returned table by the name the *user typed*, so
`job projects set arf_promoter 65` wrote `arf-promoter` and then died with
KeyError on the echo. The write had already landed; the operator saw a
traceback and had no idea whether it had.

Every existing CLI test hands the command a hand-written fake response, which
is precisely how that shipped: the fixture encodes what I believed the server
returns rather than what it returns. So these bind the real Typer command to
the real FastAPI app -- CLI-to-broker is a system boundary, and the boundary is
where the two halves' disagreement lives.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

import job_cli.cli as cli_mod


@pytest.fixture
def cli(app, monkeypatch):
    """Point the CLI's `_client()` at the real app instead of a socket."""

    class _AppClient:
        def __enter__(self):
            self._c = TestClient(app)
            return self._c

        def __exit__(self, *exc):
            self._c.close()
            return False

    monkeypatch.setattr(cli_mod, "_client", _AppClient)
    return CliRunner()


def _run(cli, *argv):
    result = cli.invoke(cli_mod.app, ["projects", *argv])
    assert result.exception is None, (
        f"`job projects {' '.join(argv)}` raised {result.exception!r}\n{result.output}"
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_setting_by_a_folded_spelling_does_not_crash_and_names_what_it_wrote(cli):
    """The regression. Pre-fix this raised KeyError('PROJECT_B') AFTER the
    server had already written project-b, so the operator got a traceback for a
    write that succeeded."""
    out = _run(cli, "set", "PROJECT_B", "90")
    assert "project-b" in out, f"must name the project it actually wrote: {out!r}"
    assert "90" in out


def test_nudging_by_a_folded_spelling_does_not_crash_and_names_what_it_wrote(cli):
    out = _run(cli, "nudge", "PROJECT_B", "5")
    assert "project-b" in out, f"must name the project it actually wrote: {out!r}"
    assert "85" in out


def test_a_folded_write_says_the_name_moved(cli):
    """Naming the resolved project is necessary but not sufficient: silently
    swapping it would leave an operator believing `PROJECT_B` is registered.
    The fold has to be visible in the output."""
    out = _run(cli, "set", "PROJECT_B", "90")
    assert "PROJECT_B" in out and "project-b" in out, (
        f"both the typed and the resolved name belong in the echo: {out!r}"
    )
    assert "folded" in out.lower(), f"the fold must be stated, not implied: {out!r}"


def test_an_exact_name_is_echoed_plainly(cli):
    """Positive control: the fold notice must not fire on an ordinary write, or
    every set would read as though it had been redirected."""
    out = _run(cli, "set", "project-b", "90")
    assert "project-b" in out and "90" in out
    assert "folded" not in out.lower(), f"unfolded write must not claim a fold: {out!r}"


def test_a_brand_new_project_is_created_under_the_typed_name(cli):
    """Second positive control: folding must not swallow a genuinely new name,
    and the echo must report the name as typed."""
    out = _run(cli, "set", "a-brand-new-project", "70")
    assert "a-brand-new-project" in out and "70" in out
    assert "folded" not in out.lower()


# --- version skew: a new CLI against an older broker -------------------------


def _echo(typed, body, capsys):
    cli_mod._echo_project_write(typed, body)
    return capsys.readouterr().out


def test_an_old_brokers_bare_table_still_prints(capsys):
    """The CLI upgrades independently of the broker, so skew runs both ways. A
    broker before 0.5.41 answers with the bare table; reading `body["project"]`
    unconditionally would reproduce this very bug pointing the other way."""
    out = _echo("project-b", {"project-b": {"priority": 90}}, capsys)
    assert "project-b" in out and "90" in out


def test_an_old_broker_that_folded_says_so_rather_than_crashing(capsys):
    """The unrecoverable case: an old broker folded the name and cannot report
    where it went. The one thing the CLI must not do is print the typed name
    beside some other project's priority and call it set."""
    out = _echo("PROJECT_B", {"project-b": {"priority": 90}}, capsys)
    assert "90" not in out, f"must not attribute another project's priority: {out!r}"
    assert "does not report" in out


def test_a_project_name_with_a_slash_cannot_reach_another_route(cli, app, sample_projects_yaml):
    """audit 2026-09-02: the CLI spliced the name into the URL path unencoded,
    so `job projects set ../reload 5` POSTed to `/reload` -- the config reload
    ran and the command then reported a write that never happened. The name
    must be percent-encoded so it can only ever address `/projects/<name>`."""
    # If a reload fires, this edit becomes visible on GET /projects.
    sample_projects_yaml.write_text(
        "projects:\n  project-b: { priority: 99 }\n  _default: { priority: 40 }\n"
    )
    result = cli.invoke(cli_mod.app, ["projects", "set", "../reload", "5"])
    assert result.exit_code != 0, result.output
    with TestClient(app) as c:
        assert c.get("/projects").json()["project-b"]["priority"] == 80, (
            "the write escaped to /reload"
        )
    # Positive control in the same test: a legitimate write still lands.
    out = _run(cli, "set", "project-b", "5")
    assert "project-b -> 5" in out
