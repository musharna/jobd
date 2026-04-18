"""CLI tests — smoke only; deeper API tests are in test_api.py."""
from typer.testing import CliRunner

from job_cli.cli import app

runner = CliRunner()


def test_cli_help():
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    assert "submit" in r.stdout
