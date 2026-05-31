"""POST /events: generic audit-event ingester for worker, hook, and MCP sources."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def _events(logs_dir: Path) -> list[dict]:
    p = logs_dir / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(r) for r in p.read_text().strip().splitlines() if r]


def test_post_events_happy_path_worker_shutdown(client, logs_dir):
    r = client.post(
        "/events",
        json={
            "source": "worker",
            "event": "worker_shutdown",
            "payload": {"host": "desktop"},
        },
    )
    assert r.status_code == 204, r.text

    rows = _events(logs_dir)
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "worker"
    assert row["event"] == "worker_shutdown"
    assert row["job_id"] is None
    assert row["project"] is None
    assert row["payload"] == {"host": "desktop"}


def test_post_events_rejects_source_broker(client):
    r = client.post(
        "/events",
        json={"source": "broker", "event": "x", "payload": {}},
    )
    assert r.status_code == 422


def test_post_events_rejects_unknown_source(client):
    r = client.post(
        "/events",
        json={"source": "rogue", "event": "x", "payload": {}},
    )
    assert r.status_code == 422


def test_post_events_missing_event_field_rejected(client):
    r = client.post("/events", json={"source": "worker", "payload": {}})
    assert r.status_code == 422


def test_post_events_ts_is_broker_stamped_iso8601(client, logs_dir):
    r = client.post(
        "/events",
        json={"source": "worker", "event": "worker_shutdown", "payload": {"host": "h"}},
    )
    assert r.status_code == 204
    row = _events(logs_dir)[0]
    ts = datetime.fromisoformat(row["ts"])
    assert ts.tzinfo is not None, "ts must carry a UTC offset"


def test_post_events_job_id_and_project_passthrough(client, logs_dir):
    r = client.post(
        "/events",
        json={
            "source": "worker",
            "event": "watchdog_fired",
            "job_id": 42,
            "project": "project-b",
            "payload": {"host": "desktop", "reason": "wall_timeout", "threshold_s": 600},
        },
    )
    assert r.status_code == 204, r.text
    row = _events(logs_dir)[0]
    assert row["job_id"] == 42
    assert row["project"] == "project-b"
    assert row["payload"] == {"host": "desktop", "reason": "wall_timeout", "threshold_s": 600}


def test_post_events_payload_nested_structures_round_trip(client, logs_dir):
    nested = {"foo": [1, 2, {"bar": "baz"}], "qux": None}
    r = client.post(
        "/events",
        json={"source": "hook", "event": "hook_blocked", "payload": nested},
    )
    assert r.status_code == 204
    row = _events(logs_dir)[0]
    assert row["source"] == "hook"
    assert row["payload"] == nested
