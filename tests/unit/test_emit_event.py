"""Schema-v2 _emit_event tests."""

from __future__ import annotations

import json
from pathlib import Path

from jobd.broker.events import _emit_event


def test_emit_event_writes_jsonl_row(tmp_path: Path) -> None:
    _emit_event(
        tmp_path,
        "job_submitted",
        source="broker",
        job_id=42,
        project="project-c",
        priority=55,
        host_pin="any",
    )
    rows = (tmp_path / "events.jsonl").read_text().strip().splitlines()
    assert len(rows) == 1
    row = json.loads(rows[0])
    assert row["event"] == "job_submitted"
    assert row["source"] == "broker"
    assert row["job_id"] == 42
    assert row["project"] == "project-c"
    assert row["payload"] == {"priority": 55, "host_pin": "any"}
    assert "ts" in row


def test_emit_event_optional_job_and_project(tmp_path: Path) -> None:
    _emit_event(tmp_path, "sweep_warning", source="broker", warning_text="queue blocked")
    row = json.loads((tmp_path / "events.jsonl").read_text().strip())
    assert row["job_id"] is None
    assert row["project"] is None
    assert row["payload"] == {"warning_text": "queue blocked"}


def test_emit_event_swallows_io_error(tmp_path: Path, monkeypatch) -> None:
    """Observability must not break broker liveness."""

    def explode(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.open", explode)
    # No exception should escape.
    _emit_event(tmp_path, "job_submitted", source="broker", job_id=1, project="p")
