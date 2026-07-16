"""`job submit --stdin` (line-per-job batch) and `job logs --follow`.

The stdin batch is simple_gpu_scheduler's whole value proposition absorbed:
one shell command per line, each an independent job sharing the invocation's
options, run via `bash -c` so pipes/redirects survive. `logs -f` is `pueue
follow` muscle memory routed onto the existing /wait SSE stream.
"""

from __future__ import annotations

import httpx
import pytest
from typer.testing import CliRunner

import job_cli.cli as cli_mod
from job_cli.cli import app

runner = CliRunner()


class _BatchClient:
    """Records every POST body; returns incrementing job ids."""

    def __init__(self, captured: list):
        self._captured = captured

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def post(self, path, *, json=None, params=None):
        self._captured.append((path, json))
        return httpx.Response(200, json={"id": 100 + len(self._captured)})


@pytest.fixture
def batch_client(monkeypatch):
    captured: list = []
    monkeypatch.setattr(cli_mod, "_client", lambda: _BatchClient(captured))
    return captured


def test_stdin_submits_one_job_per_line_skipping_blanks_and_comments(batch_client):
    stdin = "python train.py --lr 0.1\n\n# a comment\npython train.py --lr 0.01 | tee log\n"
    r = runner.invoke(app, ["submit", "-p", "proj", "--stdin"], input=stdin)
    assert r.exit_code == 0, r.output

    assert len(batch_client) == 2
    for path, body in batch_client:
        assert path == "/submit"
        assert body["project"] == "proj"
    # Lines run via bash -c so shell constructs (the pipe) survive verbatim.
    assert batch_client[0][1]["cmd"] == ["bash", "-c", "python train.py --lr 0.1"]
    assert batch_client[1][1]["cmd"] == ["bash", "-c", "python train.py --lr 0.01 | tee log"]

    # One id-per-line on stdout (composable), summary on stderr.
    out_lines = [line for line in r.output.splitlines() if line and line[0].isdigit()]
    assert out_lines[0].startswith("101\t") and out_lines[1].startswith("102\t")
    assert "Submitted 2 jobs from stdin (ids 101..102)" in r.output


def test_stdin_shares_resource_options_across_the_batch(batch_client):
    r = runner.invoke(
        app,
        ["submit", "-p", "proj", "--gpu", "--vram-required", "16", "--stdin"],
        input="echo a\necho b\n",
    )
    assert r.exit_code == 0, r.output
    for _path, body in batch_client:
        assert body["vram_gb"] == 16
        assert body["requires"]["gpu"] is True


@pytest.mark.parametrize(
    ("args", "stdin", "expect"),
    [
        (["submit", "-p", "p", "--stdin", "--", "echo", "hi"], "echo a\n", "mutually exclusive"),
        (["submit", "-p", "p", "--stdin", "--count", "2"], "echo a\n", "don't apply"),
        (["submit", "-p", "p", "--stdin", "--sweep", "x=1,2"], "echo a\n", "don't apply"),
        (["submit", "-p", "p", "--stdin", "--wait"], "echo a\n", "--wait/--explain"),
        (["submit", "-p", "p", "--stdin"], "\n# only a comment\n", "no commands on stdin"),
        (["submit", "-p", "p"], "", "Command required (or use --stdin)"),
    ],
)
def test_stdin_mode_guards(batch_client, args, stdin, expect):
    r = runner.invoke(app, args, input=stdin)
    assert r.exit_code == 2, r.output
    assert expect in r.output
    assert batch_client == []  # nothing submitted on a refused invocation


def test_logs_follow_streams_via_wait(monkeypatch):
    called: list[int] = []
    monkeypatch.setattr(cli_mod, "_stream_wait", lambda jid: called.append(jid))
    r = runner.invoke(app, ["logs", "42", "--follow"])
    assert r.exit_code == 0, r.output
    assert called == [42]

    called.clear()
    r = runner.invoke(app, ["logs", "42", "-f"])
    assert r.exit_code == 0
    assert called == [42]
