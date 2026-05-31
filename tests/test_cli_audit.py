"""Tests for `job audit` event-stream query CLI."""

from __future__ import annotations

from typing import Any

from typer.testing import CliRunner


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _row(
    *,
    ts: str = "2026-05-06T12:34:56.789012+00:00",
    source: str = "broker",
    event: str = "job_submitted",
    job_id: int | None = 42,
    project: str | None = "project-c",
    payload: dict | None = None,
) -> dict:
    return {
        "ts": ts,
        "source": source,
        "event": event,
        "job_id": job_id,
        "project": project,
        "payload": payload or {},
    }


def _patch_client(monkeypatch, rows):
    import job_cli.cli as cli_mod

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, path, params=None):
            if path == "/events":
                return _FakeResp(rows)
            return _FakeResp([])

    monkeypatch.setattr(cli_mod, "_client", lambda: FakeClient())
    return cli_mod


def test_audit_command_renders_ascii_by_default(monkeypatch):
    cli_mod = _patch_client(
        monkeypatch,
        [
            _row(event="job_submitted", job_id=100, project="project-c"),
            _row(event="job_completed", job_id=100, project="project-c"),
        ],
    )
    r = CliRunner().invoke(cli_mod.app, ["audit"])
    assert r.exit_code == 0, r.stdout
    assert "job_submitted" in r.stdout
    assert "job_completed" in r.stdout
    assert "#100" in r.stdout
    assert "project-c" in r.stdout
    # Timestamp shortened to "YYYY-MM-DD HH:MM:SS"
    assert "2026-05-06 12:34:56" in r.stdout


def test_audit_command_format_json_emits_ndjson(monkeypatch):
    cli_mod = _patch_client(
        monkeypatch,
        [
            _row(event="job_submitted", job_id=1),
            _row(event="job_running", job_id=1),
        ],
    )
    r = CliRunner().invoke(cli_mod.app, ["audit", "--format", "json"])
    assert r.exit_code == 0, r.stdout
    import json as _json

    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    assert len(lines) == 2
    parsed = [_json.loads(ln) for ln in lines]
    assert parsed[0]["event"] == "job_submitted"
    assert parsed[1]["event"] == "job_running"


def test_audit_command_filters_passthrough(monkeypatch):
    """Each filter flag is forwarded as a URL param to /events."""
    import job_cli.cli as cli_mod

    captured: dict[str, Any] = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, path, params=None):
            captured["path"] = path
            captured["params"] = params
            return _FakeResp([])

    monkeypatch.setattr(cli_mod, "_client", lambda: FakeClient())
    r = CliRunner().invoke(
        cli_mod.app,
        [
            "audit",
            "--since",
            "3d",
            "--project",
            "project-c",
            "--event",
            "job_submitted",
            "--job-id",
            "42",
            "--source",
            "broker",
            "--limit",
            "500",
        ],
    )
    assert r.exit_code == 0, r.stdout
    assert captured["path"] == "/events"
    p = captured["params"]
    assert p["since"] == "3d"
    assert p["project"] == "project-c"
    assert p["event"] == "job_submitted"
    # Typer-coerced: --job-id 42 → int 42 (NOT the string "42")
    assert p["job_id"] == 42
    assert isinstance(p["job_id"], int)
    assert p["source"] == "broker"
    assert p["limit"] == 500


def test_audit_command_rejects_bad_format(monkeypatch):
    cli_mod = _patch_client(monkeypatch, [])
    r = CliRunner().invoke(cli_mod.app, ["audit", "--format", "garbage"])
    assert r.exit_code != 0


def test_audit_command_omits_filters_when_unset(monkeypatch):
    """No project/event/job_id/source flags → those keys absent from params."""
    import job_cli.cli as cli_mod

    captured: dict[str, Any] = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, path, params=None):
            captured["params"] = params
            return _FakeResp([])

    monkeypatch.setattr(cli_mod, "_client", lambda: FakeClient())
    r = CliRunner().invoke(cli_mod.app, ["audit"])
    assert r.exit_code == 0, r.stdout
    p = captured["params"]
    assert "since" in p
    assert "limit" in p
    assert "project" not in p
    assert "event" not in p
    assert "job_id" not in p
    assert "source" not in p


def test_audit_command_handles_null_job_id_and_project(monkeypatch):
    """Rows with job_id=None / project=None render as '-' placeholders."""
    cli_mod = _patch_client(
        monkeypatch,
        [
            _row(event="worker_registered", job_id=None, project=None),
        ],
    )
    r = CliRunner().invoke(cli_mod.app, ["audit"])
    assert r.exit_code == 0, r.stdout
    assert "worker_registered" in r.stdout
    # The job/project columns should each show '-' rather than 'None'
    assert "None" not in r.stdout


def test_audit_command_source_filter_passthrough(monkeypatch):
    """--source broker forwards to params['source']='broker'."""
    import job_cli.cli as cli_mod

    captured: dict[str, Any] = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, path, params=None):
            captured["params"] = params
            return _FakeResp([])

    monkeypatch.setattr(cli_mod, "_client", lambda: FakeClient())
    r = CliRunner().invoke(cli_mod.app, ["audit", "--source", "broker"])
    assert r.exit_code == 0, r.stdout
    assert captured["params"]["source"] == "broker"


def test_audit_command_renders_payload(monkeypatch):
    """Payload dict is flattened to k=v pairs in the ascii rendering."""
    cli_mod = _patch_client(
        monkeypatch,
        [
            _row(
                event="dispatch_skip",
                payload={"reason": "no_workers", "host": "any"},
            ),
        ],
    )
    r = CliRunner().invoke(cli_mod.app, ["audit"])
    assert r.exit_code == 0, r.stdout
    assert "reason=no_workers" in r.stdout
    assert "host=any" in r.stdout
