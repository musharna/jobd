"""CLI surface for job arrays: --count, --array filter, `status A<id>`."""

import pytest
import typer
from typer.testing import CliRunner

import job_cli.cli as cli_mod
from job_cli.cli import _parse_array_token, _parse_sweep_axes
from tests.test_cli import _FakePostClient

runner = CliRunner()


class _FakeResp:
    def __init__(self, data, headers=None):
        self._data = data
        # GET /jobs sets X-Total-Count (audit 2026-07-12); a double without
        # headers isn't a faithful stand-in for the real response.
        self.headers = headers if headers is not None else {}

    def json(self):
        return self._data


class _FakeGetClient:
    """Stub supporting .get(path, params=...) for list/status array tests."""

    def __init__(self, jobs):
        self._jobs = jobs
        self.last_params = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def get(self, path, params=None):
        self.last_params = params
        if params and "array_id" in params:
            aid = params["array_id"]
            return _FakeResp([j for j in self._jobs if j.get("array_id") == aid])
        return _FakeResp(self._jobs)


# ---- _parse_array_token unit ----


def test_parse_array_token():
    assert _parse_array_token("A42") == 42
    assert _parse_array_token("a42") == 42
    assert _parse_array_token("42") is None
    assert _parse_array_token("Ax") is None
    assert _parse_array_token("A") is None
    assert _parse_array_token("") is None


# ---- submit --count ----


def test_submit_count_populates_body(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(cli_mod, "_client", lambda: _FakePostClient(captured))
    r = runner.invoke(
        cli_mod.app,
        [
            "submit",
            "--project",
            "p",
            "--cwd",
            "/tmp",
            "--count",
            "8",
            "--",
            "python",
            "t.py",
            "{i}",
        ],
    )
    assert r.exit_code == 0, r.output
    assert captured["body"]["count"] == 8
    # template passed through verbatim; broker does the substitution
    assert captured["body"]["cmd"] == ["python", "t.py", "{i}"]


def test_submit_count_one_omitted_from_body(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(cli_mod, "_client", lambda: _FakePostClient(captured))
    r = runner.invoke(
        cli_mod.app, ["submit", "--project", "p", "--cwd", "/tmp", "--", "echo", "hi"]
    )
    assert r.exit_code == 0, r.output
    assert "count" not in captured["body"]


def test_submit_array_summary_rendered(monkeypatch):
    captured: dict = {}
    resp = {"array_id": 7, "count": 3, "job_ids": [7, 8, 9], "warnings": []}
    monkeypatch.setattr(cli_mod, "_client", lambda: _FakePostClient(captured, response=resp))
    r = runner.invoke(
        cli_mod.app,
        ["submit", "--project", "p", "--cwd", "/tmp", "--count", "3", "--", "echo", "{i}"],
    )
    assert r.exit_code == 0, r.output
    assert "Submitted array A7" in r.output
    assert "ids 7..9" in r.output


# ---- list --array ----


def test_list_array_invalid_token_exits_2(monkeypatch):
    monkeypatch.setattr(cli_mod, "_client", lambda: _FakeGetClient([]))
    monkeypatch.setattr(cli_mod, "_worker_health_banner", lambda c: None)
    r = runner.invoke(cli_mod.app, ["list", "--array", "foo"])
    assert r.exit_code == 2
    assert "invalid --array" in r.output


def test_list_array_filters_and_annotates(monkeypatch):
    jobs = [
        {
            "id": 7,
            "state": "queued",
            "project": "p",
            "cmd": ["echo", "0"],
            "array_id": 7,
            "array_index": 0,
            "array_size": 2,
        },
        {
            "id": 8,
            "state": "queued",
            "project": "p",
            "cmd": ["echo", "1"],
            "array_id": 7,
            "array_index": 1,
            "array_size": 2,
        },
    ]
    fake = _FakeGetClient(jobs)
    monkeypatch.setattr(cli_mod, "_client", lambda: fake)
    monkeypatch.setattr(cli_mod, "_worker_health_banner", lambda c: None)
    r = runner.invoke(cli_mod.app, ["list", "--array", "A7"])
    assert r.exit_code == 0, r.output
    assert fake.last_params["array_id"] == 7
    assert "array: A7 #0/2" in r.output


# ---- status A<id> ----


def test_status_array_aggregate_all_completed_exit_0(monkeypatch):
    jobs = [
        {"id": 7, "state": "completed", "array_id": 7, "array_index": 0, "exit_code": 0},
        {"id": 8, "state": "completed", "array_id": 7, "array_index": 1, "exit_code": 0},
    ]
    monkeypatch.setattr(cli_mod, "_client", lambda: _FakeGetClient(jobs))
    r = runner.invoke(cli_mod.app, ["status", "A7"])
    assert r.exit_code == 0, r.output
    assert "array A7" in r.output
    assert "2/2 terminal" in r.output


def test_status_array_aggregate_with_failure_exit_1(monkeypatch):
    jobs = [
        {"id": 7, "state": "completed", "array_id": 7, "array_index": 0, "exit_code": 0},
        {"id": 8, "state": "failed", "array_id": 7, "array_index": 1, "exit_code": 1},
    ]
    monkeypatch.setattr(cli_mod, "_client", lambda: _FakeGetClient(jobs))
    r = runner.invoke(cli_mod.app, ["status", "A7"])
    assert r.exit_code == 1, r.output


def test_status_array_partial_not_terminal_exit_0(monkeypatch):
    jobs = [
        {"id": 7, "state": "completed", "array_id": 7, "array_index": 0, "exit_code": 0},
        {"id": 8, "state": "running", "array_id": 7, "array_index": 1, "exit_code": None},
    ]
    monkeypatch.setattr(cli_mod, "_client", lambda: _FakeGetClient(jobs))
    r = runner.invoke(cli_mod.app, ["status", "A7"])
    # not all terminal yet → exit 0 (no failure asserted)
    assert r.exit_code == 0, r.output
    assert "1/2 terminal" in r.output


def test_status_unknown_array_exits_1(monkeypatch):
    monkeypatch.setattr(cli_mod, "_client", lambda: _FakeGetClient([]))
    r = runner.invoke(cli_mod.app, ["status", "A999"])
    assert r.exit_code == 1
    assert "no such array" in r.output


# ---- _parse_sweep_axes unit ----


def test_parse_sweep_single_axis():
    assert _parse_sweep_axes(["lr=0.1,0.01,0.001"]) == [
        {"key": "lr", "values": ["0.1", "0.01", "0.001"]}
    ]


def test_parse_sweep_multiple_axes_preserve_order():
    out = _parse_sweep_axes(["lr=0.1,0.01", "seed=1,2,3"])
    assert out == [
        {"key": "lr", "values": ["0.1", "0.01"]},
        {"key": "seed", "values": ["1", "2", "3"]},
    ]


def test_parse_sweep_value_may_contain_equals():
    # only the first `=` separates key from values
    assert _parse_sweep_axes(["arg=a=1,b=2"]) == [{"key": "arg", "values": ["a=1", "b=2"]}]


def test_parse_sweep_single_value_is_valid():
    assert _parse_sweep_axes(["model=resnet"]) == [{"key": "model", "values": ["resnet"]}]


def test_parse_sweep_missing_equals_exits_2():
    with pytest.raises(typer.Exit) as exc:
        _parse_sweep_axes(["justkey"])
    assert exc.value.exit_code == 2


def test_parse_sweep_empty_key_exits_2():
    with pytest.raises(typer.Exit) as exc:
        _parse_sweep_axes(["=1,2"])
    assert exc.value.exit_code == 2


def test_parse_sweep_empty_values_exits_2():
    with pytest.raises(typer.Exit) as exc:
        _parse_sweep_axes(["lr="])
    assert exc.value.exit_code == 2


# ---- submit --sweep ----


def test_submit_sweep_populates_body(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(cli_mod, "_client", lambda: _FakePostClient(captured))
    r = runner.invoke(
        cli_mod.app,
        [
            "submit",
            "--project",
            "p",
            "--cwd",
            "/tmp",
            "--sweep",
            "lr=0.1,0.01",
            "--sweep",
            "seed=1,2,3",
            "--",
            "python",
            "t.py",
            "--lr",
            "{lr}",
            "--seed",
            "{seed}",
        ],
    )
    assert r.exit_code == 0, r.output
    assert captured["body"]["sweep"] == [
        {"key": "lr", "values": ["0.1", "0.01"]},
        {"key": "seed", "values": ["1", "2", "3"]},
    ]
    # template passed through verbatim; broker does the substitution
    assert captured["body"]["cmd"] == ["python", "t.py", "--lr", "{lr}", "--seed", "{seed}"]
    assert "count" not in captured["body"]


def test_submit_sweep_omitted_when_unused(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(cli_mod, "_client", lambda: _FakePostClient(captured))
    r = runner.invoke(
        cli_mod.app, ["submit", "--project", "p", "--cwd", "/tmp", "--", "echo", "hi"]
    )
    assert r.exit_code == 0, r.output
    assert "sweep" not in captured["body"]


def test_submit_sweep_with_count_exits_2(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(cli_mod, "_client", lambda: _FakePostClient(captured))
    r = runner.invoke(
        cli_mod.app,
        [
            "submit",
            "--project",
            "p",
            "--cwd",
            "/tmp",
            "--count",
            "3",
            "--sweep",
            "lr=0.1,0.01",
            "--",
            "echo",
            "hi",
        ],
    )
    assert r.exit_code == 2
    assert "mutually exclusive" in r.output
