"""Tests for the GET /events broker endpoint."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _write_event(logs_dir: Path, row: dict) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    with (logs_dir / "events.jsonl").open("a") as f:
        f.write(json.dumps(row) + "\n")


def _write_raw_line(logs_dir: Path, line: str) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    with (logs_dir / "events.jsonl").open("a") as f:
        f.write(line + "\n")


def _now_iso(offset: timedelta = timedelta(0)) -> str:
    return (datetime.now(UTC) + offset).isoformat()


def _row(
    *,
    event: str = "job_submitted",
    source: str = "broker",
    job_id: int | None = 1,
    project: str | None = "project-c",
    ts: str | None = None,
    **payload,
) -> dict:
    return {
        "ts": ts if ts is not None else _now_iso(),
        "source": source,
        "event": event,
        "job_id": job_id,
        "project": project,
        "payload": payload,
    }


def test_events_endpoint_returns_recent(client, logs_dir):
    _write_event(logs_dir, _row(event="job_submitted", job_id=1))
    _write_event(logs_dir, _row(event="job_started", job_id=1))
    resp = client.get("/events")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2
    assert [r["event"] for r in rows] == ["job_submitted", "job_started"]


def test_events_endpoint_filters_by_event(client, logs_dir):
    _write_event(logs_dir, _row(event="a", job_id=1))
    _write_event(logs_dir, _row(event="b", job_id=2))
    resp = client.get("/events", params={"event": "a"})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["event"] == "a"


def test_events_endpoint_skips_legacy_rows_without_source(client, logs_dir):
    legacy = {
        "ts": _now_iso(),
        "event": "old_format",
        "job_id": 1,
        "project": "project-c",
        "payload": {},
    }
    _write_raw_line(logs_dir, json.dumps(legacy))
    _write_event(logs_dir, _row(event="new_format", job_id=2))
    resp = client.get("/events")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["event"] == "new_format"


def test_events_endpoint_since_relative(client, logs_dir):
    _write_event(logs_dir, _row(event="old", ts=_now_iso(timedelta(days=-10))))
    _write_event(logs_dir, _row(event="recent", ts=_now_iso(timedelta(hours=-1))))
    resp = client.get("/events", params={"since": "24h"})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["event"] == "recent"


def test_events_endpoint_limit_caps_results(client, logs_dir):
    for i in range(50):
        _write_event(logs_dir, _row(event="e", job_id=i))
    resp = client.get("/events", params={"limit": 10})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 10
    # Newest-last preserved → returned slice is the last 10 (job_id 40..49)
    assert [r["job_id"] for r in rows] == list(range(40, 50))


def test_events_endpoint_filters_by_project(client, logs_dir):
    _write_event(logs_dir, _row(event="e", project="project-c", job_id=1))
    _write_event(logs_dir, _row(event="e", project="project-a", job_id=2))
    resp = client.get("/events", params={"project": "project-c"})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["project"] == "project-c"


def test_events_endpoint_filters_by_job_id(client, logs_dir):
    _write_event(logs_dir, _row(event="e", job_id=1))
    _write_event(logs_dir, _row(event="e", job_id=2))
    _write_event(logs_dir, _row(event="e", job_id=3))
    resp = client.get("/events", params={"job_id": 2})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["job_id"] == 2


def test_events_endpoint_filters_by_source(client, logs_dir):
    _write_event(logs_dir, _row(event="e", source="broker", job_id=1))
    _write_event(logs_dir, _row(event="e", source="worker", job_id=2))
    _write_event(logs_dir, _row(event="e", source="mcp", job_id=3))
    resp = client.get("/events", params={"source": "broker"})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["source"] == "broker"


def test_events_endpoint_since_iso_timestamp(client, logs_dir):
    _write_event(logs_dir, _row(event="old", ts="2026-04-01T00:00:00+00:00"))
    _write_event(logs_dir, _row(event="recent", ts="2026-05-05T00:00:00+00:00"))
    resp = client.get("/events", params={"since": "2026-05-01T00:00:00Z"})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["event"] == "recent"


def test_events_endpoint_returns_empty_when_no_log_file(client, logs_dir):
    # logs_dir intentionally not created — events.jsonl missing
    resp = client.get("/events")
    assert resp.status_code == 200
    assert resp.json() == []


def test_events_endpoint_skips_malformed_json_lines(client, logs_dir):
    _write_raw_line(logs_dir, "{not valid json")
    _write_event(logs_dir, _row(event="valid", job_id=1))
    resp = client.get("/events")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["event"] == "valid"


def test_events_endpoint_limit_clamped_to_max(client, logs_dir):
    # With limit > 10000, request 99999 — clamp to 10000
    # Write 5 rows; verify all 5 returned (since 5 < 10000)
    for i in range(5):
        _write_event(logs_dir, _row(event="e", job_id=i))
    resp = client.get("/events", params={"limit": 99999})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 5


def test_events_endpoint_invalid_since_raises_400(client, logs_dir):
    resp = client.get("/events", params={"since": "garbage"})
    assert resp.status_code == 400


def test_events_endpoint_since_zero_rejected(client, logs_dir):
    resp = client.get("/events", params={"since": "0h"})
    assert resp.status_code == 400


def test_events_endpoint_skips_unparseable_ts_when_filtering(client, logs_dir):
    """A row with a junk ts is skipped (not crashing) when since= is set."""
    _write_event(logs_dir, _row(event="bad_ts", ts="not-a-timestamp"))
    _write_event(logs_dir, _row(event="recent", ts=_now_iso(timedelta(hours=-1))))
    resp = client.get("/events", params={"since": "24h"})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["event"] == "recent"
