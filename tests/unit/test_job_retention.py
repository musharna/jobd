"""jobs-table + per-job-log retention prune (JOBD_JOB_RETENTION_DAYS).

Terminal jobs whose finished_at is older than the retention horizon — and
their per-job .log files — are deleted by the sweeper. 0 days (default) keeps
history forever, so the feature is opt-in and the rest of the suite is
unaffected.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from jobd.db import Job


def _submit(client: TestClient, project: str = "project-b") -> int:
    r = client.post(
        "/submit",
        json={"project": project, "cmd": ["true"], "cwd": "/tmp/foo", "host_pin": "any"},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _set_state(
    client: TestClient, job_id: int, state: str, *, finished_days_ago: float | None
) -> None:
    """Force a job's state and finished_at (naive UTC, matching the schema)."""
    finished = (
        None
        if finished_days_ago is None
        else datetime.now(UTC).replace(tzinfo=None) - timedelta(days=finished_days_ago)
    )
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(update(Job).where(Job.id == job_id).values(state=state, finished_at=finished))


def _exists(client: TestClient, job_id: int) -> bool:
    engine = client.app.state.engine
    with engine.begin() as conn:
        return conn.execute(select(Job.id).where(Job.id == job_id)).first() is not None


def _make_log(logs_dir: Path, job_id: int) -> Path:
    p = logs_dir / f"{job_id}.log"
    p.write_text("some job output\n")
    return p


def _pruned_events(logs_dir: Path) -> list[dict]:
    f = logs_dir / "events.jsonl"
    if not f.exists():
        return []
    rows = [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("event") == "jobs_pruned"]


# ---- default = disabled ----


def test_retention_disabled_by_default(client_logs, monkeypatch):
    monkeypatch.delenv("JOBD_JOB_RETENTION_DAYS", raising=False)
    client, logs = client_logs
    jid = _submit(client)
    _set_state(client, jid, "completed", finished_days_ago=100)
    log = _make_log(logs, jid)

    assert client.app.state.prune_old_jobs() == 0
    assert _exists(client, jid), "default (0 days) must not prune anything"
    assert log.exists()


def test_invalid_env_falls_back_to_disabled(client_logs, monkeypatch):
    monkeypatch.setenv("JOBD_JOB_RETENTION_DAYS", "garbage")
    client, logs = client_logs
    jid = _submit(client)
    _set_state(client, jid, "completed", finished_days_ago=100)

    assert client.app.state.prune_old_jobs() == 0
    assert _exists(client, jid)


# ---- pruning ----


def test_prunes_old_terminal_job_and_its_log(client_logs, monkeypatch):
    monkeypatch.setenv("JOBD_JOB_RETENTION_DAYS", "7")
    client, logs = client_logs
    jid = _submit(client)
    _set_state(client, jid, "completed", finished_days_ago=10)
    log = _make_log(logs, jid)

    n = client.app.state.prune_old_jobs()
    assert n == 1
    assert not _exists(client, jid), "terminal job past the horizon must be deleted"
    # Real-execution check: the actual log file is gone from disk.
    assert not log.exists(), "the per-job .log file must be unlinked"


def test_keeps_recent_terminal_job(client_logs, monkeypatch):
    monkeypatch.setenv("JOBD_JOB_RETENTION_DAYS", "7")
    client, _ = client_logs
    jid = _submit(client)
    _set_state(client, jid, "completed", finished_days_ago=2)  # within the window

    assert client.app.state.prune_old_jobs() == 0
    assert _exists(client, jid)


@pytest.mark.parametrize("state", ["queued", "running", "assigned"])
def test_skips_non_terminal_jobs(client_logs, monkeypatch, state):
    monkeypatch.setenv("JOBD_JOB_RETENTION_DAYS", "7")
    client, _ = client_logs
    jid = _submit(client)
    # Even with an ancient finished_at, a non-terminal job is never pruned.
    _set_state(client, jid, state, finished_days_ago=100)

    assert client.app.state.prune_old_jobs() == 0
    assert _exists(client, jid)


@pytest.mark.parametrize("state", ["completed", "failed", "cancelled", "preempted", "orphaned"])
def test_prunes_all_terminal_states(client_logs, monkeypatch, state):
    monkeypatch.setenv("JOBD_JOB_RETENTION_DAYS", "7")
    client, _ = client_logs
    jid = _submit(client)
    _set_state(client, jid, state, finished_days_ago=30)

    assert client.app.state.prune_old_jobs() == 1
    assert not _exists(client, jid)


def test_emits_jobs_pruned_event(client_logs, monkeypatch):
    monkeypatch.setenv("JOBD_JOB_RETENTION_DAYS", "7")
    client, logs = client_logs
    a, b = _submit(client), _submit(client)
    _set_state(client, a, "completed", finished_days_ago=30)
    _set_state(client, b, "failed", finished_days_ago=30)

    client.app.state.prune_old_jobs()
    events = _pruned_events(logs)
    assert len(events) == 1
    assert events[0]["source"] == "broker"
    assert events[0]["payload"]["count"] == 2
    assert events[0]["payload"]["retention_days"] == 7


def test_missing_log_file_is_not_an_error(client_logs, monkeypatch):
    """A terminal job whose .log was already gone still prunes cleanly."""
    monkeypatch.setenv("JOBD_JOB_RETENTION_DAYS", "7")
    client, _ = client_logs
    jid = _submit(client)
    _set_state(client, jid, "completed", finished_days_ago=30)  # no _make_log

    assert client.app.state.prune_old_jobs() == 1
    assert not _exists(client, jid)


# ---- wired into the sweeper ----


def test_sweep_once_runs_the_prune(client_logs, monkeypatch):
    monkeypatch.setenv("JOBD_JOB_RETENTION_DAYS", "7")
    client, logs = client_logs
    jid = _submit(client)
    _set_state(client, jid, "completed", finished_days_ago=30)
    _make_log(logs, jid)

    client.app.state.sweep_once()  # the periodic pass must invoke the prune
    assert not _exists(client, jid)
    assert not (logs / f"{jid}.log").exists()
