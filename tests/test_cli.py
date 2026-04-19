"""CLI tests — smoke only; deeper API tests are in test_api.py."""

from typer.testing import CliRunner

from job_cli.cli import app

runner = CliRunner()


def test_cli_help():
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    assert "submit" in r.stdout


def test_submit_builds_requires_from_flags(monkeypatch):
    """--needs / --arch / --os / --gpu should populate the requires block."""
    from typer.testing import CliRunner
    import httpx
    import job_cli.cli as cli_mod

    captured = {}

    def fake_post(url, json, **kw):
        captured["url"] = url
        captured["body"] = json
        return httpx.Response(200, json={"id": 1})

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setattr(cli_mod, "BASE", "http://fake")

    runner = CliRunner()
    r = runner.invoke(
        cli_mod.app,
        [
            "submit",
            "--project",
            "p",
            "--cwd",
            "/tmp",
            "--needs",
            "R",
            "--needs",
            "python3",
            "--arch",
            "arm64",
            "--gpu",
            "--idempotent",
            "--",
            "echo",
            "hi",
        ],
    )
    assert r.exit_code == 0
    body = captured["body"]
    assert body["requires"] == {
        "arch": "arm64",
        "os": "any",
        "gpu": True,
        "needs": ["R", "python3"],
        "idempotent": True,
    }
