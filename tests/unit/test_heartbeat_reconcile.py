"""SIGTERM-drain Phase 2 (docs/plans/sigterm-drain.md): heartbeat reconcile.

A worker that dies without draining (SIGKILL, crash, power loss) restarts
within seconds under Restart=on-failure and heartbeats again — refreshing the
liveness clock the dead-worker reaper keys on — while knowing nothing about
the jobs it was running. Those jobs strand in RUNNING/ASSIGNED forever.

The backstop: workers report their in-flight job ids in every heartbeat.
Any job claimed by that host, older than RECONCILE_MIN_AGE_SECONDS, and
absent from RECONCILE_MISS_THRESHOLD consecutive reports gets the
worker-died disposition (requeue ASSIGNED + idempotent RUNNING; orphan
non-idempotent RUNNING with dependency cascade). Heartbeats without the
field (old workers) reconcile nothing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from jobd import app as app_mod
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


def _hb(client: TestClient, host: str, in_flight: list[int] | None = None):
    body: dict = {
        "host": host,
        "free_vram_gb": 0,
        "unregistered_vram_gb": 0,
        "free_ram_gb": 8,
        "idle_cpus": 4,
    }
    if in_flight is not None:
        body["in_flight_job_ids"] = in_flight
    r = client.post("/heartbeat", json=body)
    assert r.status_code == 200, r.text
    return r


def _claim_job(client: TestClient, host: str, submit_payload: dict, *, start: bool = True) -> int:
    job_id = client.post("/submit", json=submit_payload).json()["id"]
    _hb(client, host)
    claim = client.post(
        "/next-job",
        json={
            "host": host,
            "free_vram_gb": 48,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 64,
            "idle_cpus": 16,
        },
    )
    assert claim.json() is not None and claim.json()["id"] == job_id, claim.text
    if start:
        started = client.post(f"/jobs/{job_id}/started")
        assert started.json()["state"] == "running", started.text
    return job_id


def _backdate_claim(job_id: int, seconds_ago: int) -> None:
    engine = app_mod._engine_for_testing()
    with engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(started_at=datetime.now(UTC) - timedelta(seconds=seconds_ago))
        )


def _misses(job_id: int) -> int:
    engine = app_mod._engine_for_testing()
    with engine.begin() as conn:
        rows = conn.execute(select(Job.reconcile_misses).where(Job.id == job_id)).all()
    return rows[0][0]


_SUBMIT = {"cmd": ["bash", "-c", "sleep 60"], "cwd": "/tmp", "project": "project-a"}
_SUBMIT_IDEM = {**_SUBMIT, "requires": {"idempotent": True}}


def test_legacy_heartbeat_without_field_reconciles_nothing(client):
    """Old workers don't report in-flight ids; their heartbeats must never
    trigger reconcile, no matter how many arrive."""
    job_id = _claim_job(client, "ghost", _SUBMIT)
    _backdate_claim(job_id, 600)
    for _ in range(4):
        _hb(client, "ghost")  # no in_flight_job_ids field
    assert client.get(f"/jobs/{job_id}").json()["state"] == "running"


def test_reported_job_survives_heartbeats(client):
    job_id = _claim_job(client, "ghost", _SUBMIT)
    _backdate_claim(job_id, 600)
    for _ in range(3):
        _hb(client, "ghost", in_flight=[job_id])
    assert client.get(f"/jobs/{job_id}").json()["state"] == "running"
    assert _misses(job_id) == 0


def test_unreported_running_job_orphaned_after_two_misses(client, tmp_path):
    """Non-idempotent RUNNING job missing from two consecutive reports gets
    the worker-died disposition: orphaned with termination_reason=
    worker_restarted, dependents cascade-cancelled, event emitted."""
    parent_id = _claim_job(client, "ghost", _SUBMIT)
    child_id = client.post("/submit", json={**_SUBMIT, "depends_on": [parent_id]}).json()["id"]
    _backdate_claim(parent_id, 600)

    _hb(client, "ghost", in_flight=[])
    assert client.get(f"/jobs/{parent_id}").json()["state"] == "running"  # one miss: debounced

    _hb(client, "ghost", in_flight=[])
    parent = client.get(f"/jobs/{parent_id}").json()
    assert parent["state"] == "orphaned", parent
    assert parent["termination_reason"] == "worker_restarted"
    assert parent["finished_at"] is not None
    assert client.get(f"/jobs/{child_id}").json()["state"] == "cancelled"

    events = [
        json.loads(line) for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
    ]
    orphaned = [e for e in events if e["event"] == "job_orphaned" and e.get("job_id") == parent_id]
    assert len(orphaned) == 1
    assert orphaned[0]["payload"]["termination_reason"] == "worker_restarted"


def test_unreported_idempotent_running_job_requeued(client):
    job_id = _claim_job(client, "ghost", _SUBMIT_IDEM)
    _backdate_claim(job_id, 600)
    _hb(client, "ghost", in_flight=[])
    _hb(client, "ghost", in_flight=[])
    got = client.get(f"/jobs/{job_id}").json()
    assert got["state"] == "queued", got
    assert got["worker"] is None
    assert got["started_at"] is None


def test_unreported_assigned_job_requeued_regardless_of_idempotency(client):
    """ASSIGNED means the workload never started (no /started), so there are
    no side effects to protect — requeue even when non-idempotent. This also
    fixes the lost-/next-job-response case, where the old reaper left the
    job ASSIGNED forever because the worker's heartbeat stayed fresh."""
    job_id = _claim_job(client, "ghost", _SUBMIT, start=False)
    assert client.get(f"/jobs/{job_id}").json()["state"] == "assigned"
    _backdate_claim(job_id, 600)
    _hb(client, "ghost", in_flight=[])
    _hb(client, "ghost", in_flight=[])
    got = client.get(f"/jobs/{job_id}").json()
    assert got["state"] == "queued", got
    assert got["worker"] is None


def test_miss_counter_resets_when_job_reported_again(client):
    """Two misses must be CONSECUTIVE — a report in between (e.g. the
    /complete-in-flight race resolving) resets the counter."""
    job_id = _claim_job(client, "ghost", _SUBMIT)
    _backdate_claim(job_id, 600)
    _hb(client, "ghost", in_flight=[])
    assert _misses(job_id) == 1
    _hb(client, "ghost", in_flight=[job_id])
    assert _misses(job_id) == 0
    _hb(client, "ghost", in_flight=[])
    assert client.get(f"/jobs/{job_id}").json()["state"] == "running"


def test_young_claim_never_accumulates_misses(client):
    """A claim younger than RECONCILE_MIN_AGE_SECONDS is inside the
    /next-job -> first-report window; unreported is not yet suspicious."""
    job_id = _claim_job(client, "ghost", _SUBMIT)  # started_at = now
    _hb(client, "ghost", in_flight=[])
    _hb(client, "ghost", in_flight=[])
    assert client.get(f"/jobs/{job_id}").json()["state"] == "running"
    assert _misses(job_id) == 0


def test_other_hosts_jobs_untouched(client):
    """Reconcile is scoped to the reporting host."""
    job_id = _claim_job(client, "ghost", _SUBMIT)
    _backdate_claim(job_id, 600)
    _hb(client, "other-host", in_flight=[])
    _hb(client, "other-host", in_flight=[])
    assert client.get(f"/jobs/{job_id}").json()["state"] == "running"
