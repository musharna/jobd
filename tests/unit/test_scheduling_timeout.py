"""S3 (runtime-zombies audit): scheduling_timeout_s — broker auto-terminates
queued jobs whose scheduling deadline elapses without a worker dispatch.

Motivated by jobs 577/578 on server (2026-05-16): capability-mismatch
jobd-self echo smokes that will never dispatch and have no DLQ. Pattern
borrowed from Hatchet's `scheduling_timeout` field.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from jobd.app import build_app
from jobd.db import Job


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


def test_scheduling_timeout_field_accepted_on_submit(client):
    r = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-a",
            "scheduling_timeout_s": 2,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["scheduling_timeout_s"] == 2


def test_scheduling_timeout_default_none_opt_in(client):
    """Pre-launch CRIT-1 fix: the default is None (opt-out). A job omitting
    scheduling_timeout_s waits indefinitely for a capable worker — a default
    300s timeout silently killed legitimate jobs queued behind long work or
    waiting on a momentarily-saturated GPU. Callers wanting a stuck-queue
    guard must opt in via an explicit value."""
    r = client.post(
        "/submit",
        json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"},
    )
    assert r.status_code == 200
    assert r.json()["scheduling_timeout_s"] is None


def test_scheduling_timeout_unset_job_never_timed_out(client):
    """A job submitted WITHOUT scheduling_timeout_s is never auto-terminated,
    no matter how long it sits queued (the sweeper's `is_not(None)` guard skips
    it). This is the behavior CRIT-1 restores."""
    r = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-a",
            # arm64 — no worker advertises it, so it'd queue forever
            "requires": {"arch": "arm64"},
        },
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]

    # Backdate submitted_at far past the old 300s default.
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(submitted_at=datetime.now(UTC) - timedelta(hours=2))
        )

    client.app.state.sweep_once()
    got = client.get(f"/jobs/{job_id}").json()
    assert got["state"] == "queued", got


def test_scheduling_timeout_terminates_unscheduled_job(client):
    """Queued job past its scheduling deadline transitions to
    scheduling_timeout terminal state after a sweep."""
    r = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-a",
            # arm64 — no worker advertises it, so it'd queue forever
            "requires": {"arch": "arm64"},
            "scheduling_timeout_s": 2,
        },
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]

    # Backdate submitted_at past the scheduling deadline.
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(submitted_at=datetime.now(UTC) - timedelta(seconds=5))
        )

    client.app.state.sweep_once()

    got = client.get(f"/jobs/{job_id}").json()
    assert got["state"] == "scheduling_timeout", got
    assert got["finished_at"] is not None
    assert got["termination_reason"] == "scheduling_timeout"


def test_scheduling_timeout_long_override_keeps_job_queued(client):
    """A queued job with an explicit long scheduling_timeout_s does NOT
    transition until the override actually elapses. Audit 2026-05-18
    spec-review: now that 300s is the default, callers needing the old
    "queue forever" behavior must opt in explicitly via a multi-day value."""
    r = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-a",
            "requires": {"arch": "arm64"},
            "scheduling_timeout_s": 7 * 24 * 3600,  # 7d — max bound
        },
    )
    job_id = r.json()["id"]

    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(submitted_at=datetime.now(UTC) - timedelta(seconds=600))
        )

    client.app.state.sweep_once()
    got = client.get(f"/jobs/{job_id}").json()
    # 600s elapsed << 7d override; still queued.
    assert got["state"] == "queued", got


def test_scheduling_timeout_not_applied_to_assigned_jobs(client):
    """Once a job is assigned/running, scheduling deadline no longer applies
    — only the dispatch-pending window is bounded."""
    client.post(
        "/heartbeat",
        json={
            "host": "w1",
            "free_vram_gb": 0,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 8,
            "idle_cpus": 4,
            "arch": "x86_64",
            "os": "linux",
            "gpu": False,
            "tags": [],
            "host_aliases": [],
        },
    )
    r = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-a",
            "scheduling_timeout_s": 1,
        },
    )
    job_id = r.json()["id"]

    # Claim it.
    claim = client.post(
        "/next-job",
        json={
            "host": "w1",
            "free_vram_gb": 0,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 8,
            "idle_cpus": 4,
        },
    )
    assert claim.json()["id"] == job_id
    assert claim.json()["state"] == "assigned"

    # Backdate submitted_at way past the deadline.
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(submitted_at=datetime.now(UTC) - timedelta(seconds=60))
        )

    client.app.state.sweep_once()
    got = client.get(f"/jobs/{job_id}").json()
    # Still assigned — sweeper does not retroactively kill assigned/running
    # jobs based on scheduling_timeout.
    assert got["state"] == "assigned", got
