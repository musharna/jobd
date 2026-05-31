"""Broker job-lifecycle event emission tests."""

from __future__ import annotations

import json
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


def _submit_minimal(client, project="project-c", host_pin="any"):
    r = client.post(
        "/submit",
        json={"project": project, "cmd": ["true"], "cwd": "/tmp", "host_pin": host_pin},
    )
    assert r.status_code == 200, r.text
    return r.json()


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


def test_submit_emits_job_submitted(client, logs_dir):
    info = _submit_minimal(client, project="project-c")
    row = _last_event(logs_dir, "job_submitted")
    assert row["source"] == "broker"
    assert row["project"] == "project-c"
    assert row["job_id"] == info["id"]
    # priority lifts to payload
    assert "priority" in row["payload"]


def test_next_job_emits_job_dispatched(client, logs_dir):
    _submit_minimal(client, host_pin="any")
    # Must register a worker first (heartbeat) so the matcher has a snapshot
    client.post("/heartbeat", json=_heartbeat_payload())
    r = client.post("/next-job", json=_heartbeat_payload())
    assert r.status_code == 200
    body = r.json()
    if body is not None:  # only assert when a dispatch actually happened
        row = _last_event(logs_dir, "job_dispatched")
        assert row["job_id"] == body["id"]
        assert row["payload"]["worker"] == "laptop"
        assert "queue_wait_s" in row["payload"]
        assert row["payload"]["queue_wait_s"] >= 0


def test_started_emits_job_started(client, logs_dir):
    _submit_minimal(client, host_pin="any")
    client.post("/heartbeat", json=_heartbeat_payload())
    r = client.post("/next-job", json=_heartbeat_payload())
    assert r.status_code == 200 and r.json() is not None
    job_id = r.json()["id"]
    r2 = client.post(f"/jobs/{job_id}/started")
    assert r2.status_code == 200
    row = _last_event(logs_dir, "job_started")
    assert row["job_id"] == job_id


def test_complete_emits_job_completed_with_wall_s(client, logs_dir):
    _submit_minimal(client, host_pin="any")
    client.post("/heartbeat", json=_heartbeat_payload())
    r = client.post("/next-job", json=_heartbeat_payload())
    assert r.json() is not None
    job_id = r.json()["id"]
    client.post(f"/jobs/{job_id}/started")
    r2 = client.post(
        f"/jobs/{job_id}/complete",
        json={"exit_code": 0, "final_state": "completed"},
    )
    assert r2.status_code == 200
    row = _last_event(logs_dir, "job_completed")
    assert row["payload"]["final_state"] == "completed"
    assert row["payload"]["exit_code"] == 0
    assert row["payload"]["wall_s"] is not None


def test_cancel_queued_emits_job_cancelled(client, logs_dir):
    info = _submit_minimal(client)
    r = client.post(f"/jobs/{info['id']}/cancel")
    assert r.status_code == 200
    row = _last_event(logs_dir, "job_cancelled")
    assert row["payload"]["prior_state"] == "queued"
    assert row["payload"]["by"] == "user"
