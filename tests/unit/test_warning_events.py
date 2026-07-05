"""Broker submit_warning + sweep_warning event tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import update


def _events(logs_dir: Path, event_name: str) -> list[dict]:
    path = logs_dir / "events.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(ln) for ln in path.read_text().strip().splitlines() if ln]
    return [r for r in rows if r["event"] == event_name]


def test_submit_with_unknown_project_emits_submit_warning(client, logs_dir):
    """Submitting against a project not in projects.yaml triggers
    unknown_project_warning, which should now also emit a submit_warning event.
    """
    r = client.post(
        "/submit",
        json={
            "project": "totally-novel-project",
            "cmd": ["true"],
            "cwd": "/tmp",
            "host_pin": "any",
        },
    )
    assert r.status_code == 200, r.text
    rows = _events(logs_dir, "submit_warning")
    assert len(rows) == 1, f"expected one submit_warning, got {len(rows)}: {rows}"
    row = rows[0]
    assert row["source"] == "broker"
    assert row["project"] == "totally-novel-project"
    assert row["job_id"] == r.json()["id"]
    assert "totally-novel-project" in row["payload"]["warning_text"]


def test_submit_with_known_project_does_not_emit_submit_warning(client, logs_dir):
    """Sanity: a clean submit with a known project + empty fleet + host_pin=any
    should NOT emit submit_warning (preflight returns None when no candidates;
    no unknown-project warning since the project is in projects.yaml)."""
    r = client.post(
        "/submit",
        json={"project": "project-b", "cmd": ["true"], "cwd": "/tmp", "host_pin": "any"},
    )
    assert r.status_code == 200, r.text
    # Sanity check: the submit itself produced no warning.
    assert r.json()["warning"] is None
    rows = _events(logs_dir, "submit_warning")
    assert len(rows) == 0, f"expected no submit_warning, got: {rows}"


def test_sweep_emits_sweep_warning_for_unmatcheable(client, logs_dir):
    """Submit a job whose requires can't be satisfied by any online worker;
    fast-forward sweep clock; expect a sweep_warning event with the
    unmatcheable text."""
    from jobd.db import Job

    # Submit a job requiring GPU; no worker registered → unmatcheable
    r = client.post(
        "/submit",
        json={
            "project": "project-b",
            "cmd": ["true"],
            "cwd": "/tmp",
            "host_pin": "any",
            "requires": {"arch": "arm64"},
            "scheduling_timeout_s": 7 * 24 * 3600,
        },
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]

    # Backdate submitted_at so the unmatcheable threshold (60s) trips.
    eng = client.app.state.engine
    with eng.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(submitted_at=datetime.now(UTC) - timedelta(minutes=5))
        )

    client.app.state.sweep_once()

    rows = [r for r in _events(logs_dir, "sweep_warning") if r["job_id"] == job_id]
    assert rows, (
        f"expected sweep_warning for job {job_id}, got: {_events(logs_dir, 'sweep_warning')}"
    )
    text = rows[-1]["payload"]["warning_text"]
    assert "advertise" in text or "unmatcheable" in text.lower(), (
        f"unexpected warning_text: {text!r}"
    )
    assert rows[-1]["source"] == "broker"
    assert rows[-1]["project"] == "project-b"


def test_sweep_does_not_re_emit_same_warning(client, logs_dir):
    """Two sweep ticks against the same unmatcheable job emit only ONE
    sweep_warning event — the second tick sees `j.warning != new_w` is false
    and skips."""
    from jobd.db import Job

    r = client.post(
        "/submit",
        json={
            "project": "project-b",
            "cmd": ["true"],
            "cwd": "/tmp",
            "host_pin": "any",
            "requires": {"arch": "arm64"},
            "scheduling_timeout_s": 7 * 24 * 3600,
        },
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    eng = client.app.state.engine
    with eng.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(submitted_at=datetime.now(UTC) - timedelta(minutes=5))
        )
    client.app.state.sweep_once()
    client.app.state.sweep_once()
    rows = [r for r in _events(logs_dir, "sweep_warning") if r["job_id"] == job_id]
    assert len(rows) == 1, f"expected ONE sweep_warning across two ticks, got {len(rows)}: {rows}"
