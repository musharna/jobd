"""audit 2026-09-02: `GET /projects` (and so `job projects list`) never
serialised `roots`, so no runtime surface showed which directories the broker
had actually loaded -- the only way to learn a root was to submit a dry run
from inside it. The overlay writer is unaffected: it persists priorities only.
"""

from starlette.testclient import TestClient
from typer.testing import CliRunner

import job_cli.cli as cli_mod


def test_get_projects_reports_each_projects_roots(rooted_client):
    body = rooted_client.get("/projects").json()
    assert body["beta"]["roots"] == ["/home/user/beta"]
    assert body["gamma"]["roots"] == ["/home/user/gamma"]
    # A project without roots does not grow an empty key.
    assert "roots" not in body["_default"]


def test_job_projects_list_renders_roots(rooted_app, monkeypatch):
    class _AppClient:
        def __enter__(self):
            self._c = TestClient(rooted_app)
            return self._c

        def __exit__(self, *exc):
            self._c.close()
            return False

    monkeypatch.setattr(cli_mod, "_client", _AppClient)
    r = CliRunner().invoke(cli_mod.app, ["projects", "list"])
    assert r.exit_code == 0, r.output
    beta_line = next(line for line in r.output.splitlines() if "beta" in line)
    assert "roots=/home/user/beta" in beta_line
