"""Dry-run submit: validate + plan-route without inserting a job row.

Design convention — preview/dry-run mode:

> Any tool whose default behavior mutates user-visible state must support
> a preview mode. The preview path must exercise the same logic as the
> live path (no stub returns).

For `/submit` that means: profile resolution, project-defaults resolution,
cwd sanity check, depends_on existence check, requires resolution,
preflight/contention warnings — all run; only the `session.add` /
`session.commit` and the `job_submitted` event are skipped.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jobd.app import build_app


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


def test_submit_dry_run_does_not_queue(client):
    """Core contract: dry_run=true returns the would-be plan; no Job row created."""
    r = client.post(
        "/submit",
        json={
            "cmd": ["bash", "-c", "echo hi"],
            "cwd": "/tmp",
            "project": "project-a",
            "dry_run": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "dry-run"
    # Plan surfaces what the live submit path would have decided.
    assert "would_route_to" in body
    assert "would_use_worker" in body
    assert "validation" in body
    # Project resolution still ran: project-a base priority is 55.
    assert body["validation"]["effective_priority"] == 55

    # Confirm no actual queue entry was created.
    list_resp = client.get("/jobs?project=project-a")
    assert list_resp.status_code == 200
    assert list_resp.json() == []


def test_submit_dry_run_runs_cwd_validation(client):
    """Dry-run must enforce the same cwd sanity gate as a live submit —
    /mnt/c/... without --host laptop is rejected at preview time too."""
    r = client.post(
        "/submit",
        json={
            "cmd": ["bash", "-c", "echo hi"],
            "cwd": "/mnt/c/work",
            "project": "project-a",
            "dry_run": True,
        },
    )
    assert r.status_code == 400
    assert "/mnt/c/" in r.json()["detail"]


def test_submit_dry_run_runs_depends_on_validation(client):
    """Dry-run must reject depends_on pointing at a missing job."""
    r = client.post(
        "/submit",
        json={
            "cmd": ["bash", "-c", "echo hi"],
            "cwd": "/tmp",
            "project": "project-a",
            "depends_on": [9999],
            "dry_run": True,
        },
    )
    assert r.status_code == 400
    assert "depends_on" in r.json()["detail"]


def test_submit_default_dry_run_false_still_queues(client):
    """Regression guard: omitting dry_run keeps the live behavior — Job is queued."""
    r = client.post(
        "/submit",
        json={
            "cmd": ["bash", "-c", "echo hi"],
            "cwd": "/tmp",
            "project": "project-a",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "queued"
    assert body["id"] > 0
