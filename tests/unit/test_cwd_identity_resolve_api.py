def test_resolve_reports_the_root_that_supplied_the_identity(rooted_client):
    """/resolve is the dry-run surface: if it cannot say WHY a project was
    chosen, an operator debugging a surprise priority has nothing to read."""
    r = rooted_client.post(
        "/resolve",
        json={
            "cmd": ["true"],
            "cwd": "/home/mjarnold/jepagame/sweeps",
            "project": "pillar2a1_sweep",
        },
    )
    body = r.json()
    assert body["project"] == "jepagame"
    assert body["project_label"] == "pillar2a1_sweep"
    assert body["matched_root"] == "/home/mjarnold/jepagame"


def test_resolve_reports_no_root_for_a_registered_name(rooted_client):
    r = rooted_client.post(
        "/resolve",
        json={"cmd": ["true"], "cwd": "/home/mjarnold/jepagame", "project": "jepagame"},
    )
    assert r.json()["matched_root"] is None


def _explain(monkeypatch, rooted_client, argv):
    """Run `job submit --explain ...` against the in-process rooted app."""
    from typer.testing import CliRunner

    import job_cli.cli as cli_mod

    class _Fake:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            pass

        def post(self, path, *, json=None, params=None):
            assert path == "/resolve"
            return rooted_client.post("/resolve", json=json)

    monkeypatch.setattr(cli_mod, "_client", lambda: _Fake())
    return CliRunner().invoke(cli_mod.app, ["submit", *argv, "--explain", "--", "./run.sh"])


def test_explain_shows_the_root_and_the_typed_label(rooted_client, monkeypatch):
    """`--explain` is the operator's dry run. /resolve already carries
    `matched_root` and `project_label`; rendering only `project` means the one
    surface built to answer "why did this get that priority?" cannot."""
    r = _explain(
        monkeypatch,
        rooted_client,
        ["--project", "pillar2a1_sweep", "--cwd", "/home/mjarnold/jepagame/sweeps"],
    )
    assert r.exit_code == 0, r.output
    assert "pillar2a1_sweep" in r.output
    assert "/home/mjarnold/jepagame" in r.output


def test_explain_says_nothing_about_roots_when_cwd_was_not_consulted(rooted_client, monkeypatch):
    """Positive control: a registered name resolves by rule 1, so there is no
    root to name and the extra line must not appear."""
    r = _explain(
        monkeypatch,
        rooted_client,
        ["--project", "jepagame", "--cwd", "/home/mjarnold/jepagame"],
    )
    assert r.exit_code == 0, r.output
    assert "resolved config for project jepagame" in r.output
    assert "root" not in r.output
