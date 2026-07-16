"""Split retention: logs and job rows prune on independent clocks (2026-07-12).

Measured on the live broker: 2,875 job rows = 6.4 MB, but 2,605 log files =
2.0 GB (~0.7 GB/month). A row is ~2 KB and feeds the ETA estimator's per-project
p50/p90 — cheap, and more useful with age. A log is ~800 KB and is
write-once-read-maybe. One shared clock forced a false choice between keeping
8 GB/yr of logs and discarding the cheap history the estimator runs on, which is
exactly why retention was left off. Two clocks remove the choice.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import update

from jobd.broker.sweeper import prune_old_logs
from jobd.db import Job


def _finished_job(app, client, *, age_days: int, log_text: str = "hello") -> int:
    jid = client.post(
        "/submit", json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"}
    ).json()["id"]
    (app.state.shared["logs_dir"] / f"{jid}.log").write_text(log_text)
    with app.state.engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == jid)
            .values(
                state="completed",
                exit_code=0,
                finished_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=age_days),
            )
        )
    return jid


def test_old_log_is_unlinked_but_the_row_survives(app, monkeypatch):
    """The whole point: reclaim the 2 GB of logs, keep the 6 MB of history."""
    monkeypatch.setenv("JOBD_LOG_RETENTION_DAYS", "60")
    client = TestClient(app)
    logs_dir = app.state.shared["logs_dir"]
    old = _finished_job(app, client, age_days=90)

    assert prune_old_logs(app.state.SessionLocal, logs_dir) == 1

    assert not (logs_dir / f"{old}.log").exists()  # log gone
    got = client.get(f"/jobs/{old}")
    assert got.status_code == 200  # row survives
    assert got.json()["state"] == "completed"  # ...with its history intact


def test_recent_log_is_kept(app, monkeypatch):
    monkeypatch.setenv("JOBD_LOG_RETENTION_DAYS", "60")
    client = TestClient(app)
    logs_dir = app.state.shared["logs_dir"]
    recent = _finished_job(app, client, age_days=5)

    assert prune_old_logs(app.state.SessionLocal, logs_dir) == 0
    assert (logs_dir / f"{recent}.log").exists()


def test_zero_disables_log_retention(app, monkeypatch):
    monkeypatch.setenv("JOBD_LOG_RETENTION_DAYS", "0")
    client = TestClient(app)
    logs_dir = app.state.shared["logs_dir"]
    old = _finished_job(app, client, age_days=999)

    assert prune_old_logs(app.state.SessionLocal, logs_dir) == 0
    assert (logs_dir / f"{old}.log").exists()


def test_pruned_job_is_not_rescanned(app, monkeypatch):
    """log_pruned_at makes the scan shrink monotonically — otherwise every sweep
    would re-stat every historical log forever."""
    monkeypatch.setenv("JOBD_LOG_RETENTION_DAYS", "60")
    client = TestClient(app)
    logs_dir = app.state.shared["logs_dir"]
    _finished_job(app, client, age_days=90)

    assert prune_old_logs(app.state.SessionLocal, logs_dir) == 1
    # Second pass finds no candidates at all (not merely "nothing to unlink").
    assert prune_old_logs(app.state.SessionLocal, logs_dir) == 0


def test_output_reports_pruned_not_empty(app, monkeypatch):
    """A pruned log and a never-written one both leave no file — but reporting a
    pruned log as empty output makes a job that emitted megabytes look like it
    produced nothing."""
    monkeypatch.setenv("JOBD_LOG_RETENTION_DAYS", "60")
    client = TestClient(app)
    logs_dir = app.state.shared["logs_dir"]

    pruned = _finished_job(app, client, age_days=90, log_text="lots of output")
    never = _finished_job(app, client, age_days=1)
    (logs_dir / f"{never}.log").unlink()  # worker never captured anything

    prune_old_logs(app.state.SessionLocal, logs_dir)

    a = client.get(f"/jobs/{pruned}/output").json()
    assert a["size_bytes"] == 0 and a["pruned"] is True and a["pruned_at"]

    b = client.get(f"/jobs/{never}/output").json()
    assert b["size_bytes"] == 0 and b["pruned"] is False


def test_row_retention_stays_off_by_default(app, monkeypatch):
    """Rows are ~7 MB/yr and feed the estimator, so JOBD_JOB_RETENTION_DAYS
    remains opt-in even though logs now prune by default."""
    monkeypatch.delenv("JOBD_JOB_RETENTION_DAYS", raising=False)
    monkeypatch.setenv("JOBD_LOG_RETENTION_DAYS", "60")
    client = TestClient(app)
    old = _finished_job(app, client, age_days=999)

    app.state.sweep_once()

    assert client.get(f"/jobs/{old}").status_code == 200  # row kept
    assert not (app.state.shared["logs_dir"] / f"{old}.log").exists()  # log pruned
