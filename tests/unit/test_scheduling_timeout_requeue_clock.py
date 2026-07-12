"""M-1 (audit 2026-07-10): scheduling_timeout keys on last_enqueued_at, which
is reset on every requeue — a reclaimed long-running job is not killed by a
timeout measured from its original submit.

Before the fix the sweeper keyed deadline_age on submitted_at, which requeue
paths copied forward. A job with scheduling_timeout_s=300 dispatched at t=10s,
ran 2h, then had its worker die → reclaim requeued it keeping submitted_at → the
next sweep saw age≈2h > 300 → SCHEDULING_TIMEOUT + cascade-wiped dependents. A
transient worker death became a permanent failure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from jobd.app import build_app
from jobd.broker.state import _requeue_or_honor_cancel
from jobd.db import Job
from jobd.models import JobState


@pytest.fixture
def client(tmp_path, sample_projects_yaml, sample_profiles_yaml, sample_classifier_yaml):
    app = build_app(
        db_url=f"sqlite:///{tmp_path}/jobd.db",
        projects_path=sample_projects_yaml,
        profiles_path=sample_profiles_yaml,
        classifier_path=sample_classifier_yaml,
        logs_path=tmp_path / "logs",
    )
    return TestClient(app)


def _submit_unschedulable(client, timeout_s=30):
    r = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-a",
            "requires": {"arch": "arm64"},  # no worker advertises it
            "scheduling_timeout_s": timeout_s,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_recent_requeue_survives_old_submit(client):
    """submitted_at far past the deadline, but last_enqueued_at recent (just
    requeued) → the job must NOT time out."""
    job_id = _submit_unschedulable(client, timeout_s=30)
    now = datetime.now(UTC)
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                submitted_at=now - timedelta(hours=2),  # original submit long ago
                last_enqueued_at=now - timedelta(seconds=1),  # freshly requeued
            )
        )

    client.app.state.sweep_once()
    assert client.get(f"/jobs/{job_id}").json()["state"] == "queued"


def test_old_last_enqueued_still_times_out(client):
    """Control: when last_enqueued_at is also past the deadline the timeout
    still fires (no regression of the guard)."""
    job_id = _submit_unschedulable(client, timeout_s=2)
    now = datetime.now(UTC)
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                submitted_at=now - timedelta(hours=2),
                last_enqueued_at=now - timedelta(seconds=10),
            )
        )

    client.app.state.sweep_once()
    got = client.get(f"/jobs/{job_id}").json()
    assert got["state"] == "scheduling_timeout", got


def test_null_last_enqueued_falls_back_to_submitted_at(client):
    """Pre-migration rows have last_enqueued_at NULL → the clock falls back to
    submitted_at (old behavior preserved)."""
    job_id = _submit_unschedulable(client, timeout_s=2)
    now = datetime.now(UTC)
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(submitted_at=now - timedelta(seconds=10), last_enqueued_at=None)
        )

    client.app.state.sweep_once()
    assert client.get(f"/jobs/{job_id}").json()["state"] == "scheduling_timeout"


def test_requeue_helper_resets_last_enqueued_at(client):
    """The requeue helper (through which all requeue paths funnel) stamps a
    fresh last_enqueued_at."""
    job_id = _submit_unschedulable(client, timeout_s=30)
    engine = client.app.state.engine
    old = datetime(2020, 1, 1)
    # Put the job in ASSIGNED with a stale clock, as a dispatched job would be.
    with engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(state=JobState.ASSIGNED.value, last_enqueued_at=old, worker="w1")
        )

    fresh = datetime(2026, 7, 10, 12, 0, 0)
    with Session(engine) as s:
        outcome = _requeue_or_honor_cancel(
            s,
            job_id,
            (JobState.ASSIGNED,),
            now=fresh,
            state=JobState.QUEUED.value,
            worker=None,
            started_at=None,
        )
        s.commit()
    assert outcome == "requeued"

    with Session(engine) as s:
        job = s.get(Job, job_id)
        assert job is not None
        assert job.state == JobState.QUEUED.value
        assert job.last_enqueued_at == fresh  # reset, not left at `old`


def test_refuse_admission_requeue_preserves_clock(client):
    """audit 2026-07-12: a refuse-admission re-route (reset_queue_clock=False)
    must NOT stamp a fresh clock — the job never ran, so its scheduling_timeout
    must keep counting or a job oscillating on gpu_contention would evade the
    timeout forever."""
    job_id = _submit_unschedulable(client, timeout_s=30)
    engine = client.app.state.engine
    old = datetime(2020, 1, 1)
    with engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(state=JobState.ASSIGNED.value, last_enqueued_at=old, worker="w1")
        )

    fresh = datetime(2026, 7, 12, 12, 0, 0)
    with Session(engine) as s:
        outcome = _requeue_or_honor_cancel(
            s,
            job_id,
            (JobState.ASSIGNED,),
            now=fresh,
            reset_queue_clock=False,
            state=JobState.QUEUED.value,
            worker=None,
            started_at=None,
        )
        s.commit()
    assert outcome == "requeued"

    with Session(engine) as s:
        job = s.get(Job, job_id)
        assert job is not None
        assert job.state == JobState.QUEUED.value
        assert job.last_enqueued_at == old  # preserved, NOT reset to `fresh`
