"""SCHEDULING_TIMEOUT is a failed-side terminal for the depends_on cascade.

A parent that the sweeper expires into scheduling_timeout must unblock an
any_exit child (it reached a terminal) and cascade-cancel a default-policy
child (its output never materialized). Before the fix, SCHEDULING_TIMEOUT was
missing from `_DEPENDS_TERMINAL_ANY` and the cascade trigger guard, so both
children sat QUEUED forever.
"""

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


def _submit(client, depends_on=None, any_exit=False):
    body = {"cmd": ["true"], "cwd": "/tmp", "project": "project-a"}
    if depends_on is not None:
        body["depends_on"] = depends_on
    if any_exit:
        body["depends_on_any_exit"] = True
    return client.post("/submit", json=body)


def _heartbeat(client, host="w1"):
    client.post(
        "/heartbeat",
        json={
            "host": host,
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


def _next_job(client, host="w1"):
    return client.post(
        "/next-job",
        json={
            "host": host,
            "free_vram_gb": 0,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 8,
            "idle_cpus": 4,
        },
    )


def _expire_parent_to_scheduling_timeout(client, parent_id):
    """Backdate the parent's submitted_at past a 1s scheduling_timeout_s and
    sweep, so the sweeper transitions it to scheduling_timeout."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from jobd import app as app_mod
    from jobd.db import Job

    engine = app_mod._engine_for_testing()
    with engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == parent_id)
            .values(
                scheduling_timeout_s=1,
                submitted_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=60),
            )
        )
    app_mod._sweep_once()


def test_scheduling_timeout_parent_unblocks_any_exit_child(client):
    parent = _submit(client).json()
    child = _submit(client, depends_on=[parent["id"]], any_exit=True).json()

    _expire_parent_to_scheduling_timeout(client, parent["id"])

    assert client.get(f"/jobs/{parent['id']}").json()["state"] == "scheduling_timeout"
    row = client.get(f"/jobs/{child['id']}").json()
    assert row["state"] == "queued"
    # depends_on_any_exit=True: the terminal parent satisfies deps, so the
    # matcher dispatches the child.
    _heartbeat(client)
    claim = _next_job(client).json()
    assert claim["id"] == child["id"]


def test_scheduling_timeout_parent_cascade_cancels_default_child(client, tmp_path):
    parent = _submit(client).json()
    child = _submit(client, depends_on=[parent["id"]]).json()

    _expire_parent_to_scheduling_timeout(client, parent["id"])
    assert client.get(f"/jobs/{parent['id']}").json()["state"] == "scheduling_timeout"

    # Default-policy child: the parent never produced its output, so a cascade
    # over the scheduling_timeout parent must cancel it.
    from sqlalchemy.orm import sessionmaker

    from jobd import app as app_mod
    from jobd.db import Job

    SessionLocal = sessionmaker(app_mod._engine_for_testing(), expire_on_commit=False)
    with SessionLocal() as session:
        parent_row = session.get(Job, parent["id"])
        cancelled = app_mod._cascade_on_parent_terminal(session, parent_row, tmp_path / "logs")
        session.commit()

    cancelled_ids = [c[0] if isinstance(c, tuple) else c for c in cancelled]
    assert child["id"] in cancelled_ids, cancelled
    row = client.get(f"/jobs/{child['id']}").json()
    assert row["state"] == "cancelled"
    assert "parent_failed" in (row.get("warning") or "")
