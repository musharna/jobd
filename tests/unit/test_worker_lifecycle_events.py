"""Worker lifecycle event emission tests (worker_registered + worker_offline)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _last_event(logs_dir: Path, event_name: str) -> dict:
    rows = (logs_dir / "events.jsonl").read_text().strip().splitlines()
    for raw in reversed(rows):
        row = json.loads(raw)
        if row["event"] == event_name:
            return row
    raise AssertionError(f"no {event_name} event found")


def _all_events(logs_dir: Path, event_name: str) -> list[dict]:
    rows = (logs_dir / "events.jsonl").read_text().strip().splitlines()
    return [json.loads(r) for r in rows if json.loads(r)["event"] == event_name]


def _heartbeat_payload(host="laptop"):
    return {
        "host": host,
        "host_aliases": [host, "any"],
        "free_vram_gb": 0,
        "unregistered_vram_gb": 0,
        "free_ram_gb": 32,
        "idle_cpus": 8,
        "arch": "x86_64",
        "os": "linux",
        "gpu": False,
        "tags": [],
        "mount_roots": ["/tmp", "/home"],
    }


def test_first_heartbeat_emits_worker_registered(client, logs_dir):
    r = client.post("/heartbeat", json=_heartbeat_payload(host="brand-new-host"))
    assert r.status_code == 200

    row = _last_event(logs_dir, "worker_registered")
    assert row["source"] == "broker"
    assert row["job_id"] is None
    assert row["project"] is None
    payload = row["payload"]
    assert payload["host"] == "brand-new-host"
    assert payload["arch"] == "x86_64"
    assert payload["os"] == "linux"
    assert payload["gpu"] is False
    assert "host_aliases" in payload
    assert "tags" in payload
    assert "mount_roots" in payload


def test_repeat_heartbeat_does_not_re_register(client, logs_dir):
    client.post("/heartbeat", json=_heartbeat_payload(host="laptop"))
    client.post("/heartbeat", json=_heartbeat_payload(host="laptop"))
    client.post("/heartbeat", json=_heartbeat_payload(host="laptop"))

    rows = _all_events(logs_dir, "worker_registered")
    laptop_rows = [r for r in rows if r["payload"]["host"] == "laptop"]
    assert len(laptop_rows) == 1, (
        f"expected exactly one worker_registered for laptop; got {laptop_rows}"
    )


def test_distinct_hosts_each_register_once(client, logs_dir):
    client.post("/heartbeat", json=_heartbeat_payload(host="laptop"))
    client.post("/heartbeat", json=_heartbeat_payload(host="desktop"))
    client.post("/heartbeat", json=_heartbeat_payload(host="laptop"))  # repeat
    client.post("/heartbeat", json=_heartbeat_payload(host="desktop"))  # repeat

    rows = _all_events(logs_dir, "worker_registered")
    hosts = sorted(r["payload"]["host"] for r in rows)
    assert hosts == ["desktop", "laptop"]


def test_sweep_marks_workers_offline_emits_event(client, logs_dir):
    import jobd.app as cli_mod
    from sqlalchemy import update

    from jobd.db import Worker

    client.post("/heartbeat", json=_heartbeat_payload(host="host-a"))

    # Backdate heartbeat past the offline threshold (120s).
    # SQLite stores naive UTC; match the convention used in app.py:1039.
    SessionLocal = client.app.state.SessionLocal  # type: ignore[attr-defined]
    stale = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=300)
    with SessionLocal() as s:
        s.execute(update(Worker).where(Worker.host == "host-a").values(last_heartbeat=stale))
        s.commit()

    cli_mod._sweep_once()  # type: ignore[attr-defined]

    rows = _all_events(logs_dir, "worker_offline")
    host_a_rows = [r for r in rows if r["payload"]["host"] == "host-a"]
    assert len(host_a_rows) == 1, (
        f"expected exactly one worker_offline for host-a; got {host_a_rows}"
    )
    row = host_a_rows[0]
    assert row["source"] == "broker"
    assert row["job_id"] is None
    assert row["project"] is None
    # last_heartbeat should be an ISO-format string (or None defensively).
    last_hb = row["payload"]["last_heartbeat"]
    assert last_hb is not None
    # round-trip parse
    datetime.fromisoformat(last_hb)


def test_sweep_does_not_re_emit_for_already_offline_worker(client, logs_dir):
    import jobd.app as cli_mod
    from sqlalchemy import update

    from jobd.db import Worker

    client.post("/heartbeat", json=_heartbeat_payload(host="host-b"))

    SessionLocal = client.app.state.SessionLocal  # type: ignore[attr-defined]
    stale = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=300)
    with SessionLocal() as s:
        s.execute(update(Worker).where(Worker.host == "host-b").values(last_heartbeat=stale))
        s.commit()

    cli_mod._sweep_once()  # type: ignore[attr-defined]
    cli_mod._sweep_once()  # type: ignore[attr-defined]
    cli_mod._sweep_once()  # type: ignore[attr-defined]

    rows = _all_events(logs_dir, "worker_offline")
    host_b_rows = [r for r in rows if r["payload"]["host"] == "host-b"]
    assert len(host_b_rows) == 1, (
        f"expected exactly one worker_offline emission per offline transition; got {host_b_rows}"
    )
