"""CLI ergonomics (audit 2026-07-12).

Three rough edges a daily user hit every time: `job --version` errored, a bare
`job` printed a usage error instead of help, and `job list` dumped the broker's
entire job history with no cap and no indication anything was withheld.
"""

from __future__ import annotations

from datetime import UTC, datetime

from typer.testing import CliRunner

import job_cli.cli as cli_mod
from jobd import __version__


class _Resp:
    def __init__(self, data, headers=None):
        self._data = data
        self.headers = headers or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _fake_client(jobs, total, capture):
    recent = datetime.now(UTC).isoformat()
    workers = [{"host": "desktop", "last_heartbeat": recent, "state": "online"}]

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, path, params=None):
            if path == "/workers":
                return _Resp(workers)
            capture["params"] = params or {}
            limit = (params or {}).get("limit")
            page = jobs[:limit] if limit else jobs
            return _Resp(page, {"X-Total-Count": str(total)})

    return FakeClient


def _jobs(n):
    return [
        {"id": i, "state": "completed", "project": "project-a", "cmd": ["echo", str(i)]}
        for i in range(n, 0, -1)
    ]


def test_version_flag_reports_the_version():
    r = CliRunner().invoke(cli_mod.app, ["--version"])
    assert r.exit_code == 0
    assert __version__ in r.stdout


def test_bare_job_shows_the_full_help():
    """Bare `job` used to print a terse usage error with no command list. It now
    prints the full help. (Exit code stays 2 — Click's convention for "no command
    given" — so scripts can still detect it; the fix is that the user now sees
    what the commands ARE.)"""
    r = CliRunner().invoke(cli_mod.app, [])
    assert "Usage:" in r.stdout
    assert "submit" in r.stdout  # the command list is what was missing
    assert "list" in r.stdout


def test_help_documents_jobd_url():
    """JOBD_URL is the single most important setting and appeared nowhere."""
    r = CliRunner().invoke(cli_mod.app, ["--help"])
    assert r.exit_code == 0
    assert "JOBD_URL" in r.stdout


def test_list_caps_by_default_and_says_what_it_withheld(monkeypatch):
    capture: dict = {}
    monkeypatch.setattr(cli_mod, "_client", lambda: _fake_client(_jobs(200), 200, capture)())
    r = CliRunner().invoke(cli_mod.app, ["list"])
    assert r.exit_code == 0
    assert capture["params"]["limit"] == cli_mod._LIST_DEFAULT_LIMIT
    # Never truncate silently.
    assert "showing 50 of 200" in r.stdout
    assert "--all" in r.stdout


def test_list_all_removes_the_cap(monkeypatch):
    capture: dict = {}
    monkeypatch.setattr(cli_mod, "_client", lambda: _fake_client(_jobs(200), 200, capture)())
    r = CliRunner().invoke(cli_mod.app, ["list", "--all"])
    assert r.exit_code == 0
    assert "limit" not in capture["params"]
    assert "showing" not in r.stdout  # nothing withheld -> no footer


def test_list_explicit_limit_is_honoured(monkeypatch):
    capture: dict = {}
    monkeypatch.setattr(cli_mod, "_client", lambda: _fake_client(_jobs(200), 200, capture)())
    r = CliRunner().invoke(cli_mod.app, ["list", "--limit", "5"])
    assert r.exit_code == 0
    assert capture["params"]["limit"] == 5
    assert "showing 5 of 200" in r.stdout


def test_array_listing_is_not_silently_truncated(monkeypatch):
    """A truncated array misrepresents its shape, so --array shows all members."""
    capture: dict = {}
    monkeypatch.setattr(cli_mod, "_client", lambda: _fake_client(_jobs(80), 80, capture)())
    r = CliRunner().invoke(cli_mod.app, ["list", "--array", "A7"])
    assert r.exit_code == 0
    assert "limit" not in capture["params"]
