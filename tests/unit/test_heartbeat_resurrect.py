"""The REVERSE heartbeat reconcile: a job the worker is demonstrably still running.

2026-07-14, production. The laptop worker's heartbeat lapsed (a WSL freeze). The
sweeper did exactly its job and orphaned the in-flight job as `worker_died`. But
the worker had not died — it came back, and it had never stopped running the job.

It then reported that job in `in_flight_job_ids` on EVERY heartbeat, for three
hours, and the broker ignored it every single time — because `_reconcile_worker_in_flight`
only ever queried rows it already believed were ASSIGNED or RUNNING. Terminal rows
were outside its WHERE clause. Reported ids matching no such row were dropped on
the floor.

So four hours of GPU folds (55 compounds, 52 of them done) ran to completion, and
the `/complete` was discarded by the terminal-is-terminal CAS. The broker's ledger
said the job died at 17:56. The GPU said otherwise.

The reconcile answered "the broker claims this is running — is it?" and never the
mirror question, "the WORKER claims this is running — is it?". Same shape as the
`_default.defaults` bug the same day: a correct predicate pointed at the wrong set.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

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


_SUBMIT = {"cmd": ["bash", "-c", "sleep 60"], "cwd": "/tmp", "project": "project-a"}


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


def _claim(client: TestClient, host: str, payload: dict | None = None) -> int:
    job_id = client.post("/submit", json=payload or _SUBMIT).json()["id"]
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
    assert claim.json() and claim.json()["id"] == job_id, claim.text
    assert client.post(f"/jobs/{job_id}/started").json()["state"] == "running"
    return job_id


def _force(client: TestClient, job_id: int, **values) -> None:
    """Put a row into the exact state the sweeper/cascade would have left."""
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(update(Job).where(Job.id == job_id).values(**values))


def _state(client: TestClient, job_id: int) -> str:
    return client.get(f"/jobs/{job_id}").json()["state"]


def _row(client: TestClient, job_id: int) -> Job:
    engine = client.app.state.engine
    with engine.begin() as conn:
        return conn.execute(
            select(Job.state, Job.termination_reason, Job.finished_at, Job.warning).where(
                Job.id == job_id
            )
        ).one()


# --- the regression -------------------------------------------------------


@pytest.mark.parametrize("reason", ["worker_died", "worker_restarted"])
def test_a_reported_orphan_is_resurrected(client, reason):
    """THE bug. The worker says it is running the job. That falsifies the only
    premise the orphaning rested on — that the worker was gone."""
    job_id = _claim(client, "laptop")
    _force(client, job_id, state="orphaned", termination_reason=reason)
    assert _state(client, job_id) == "orphaned"

    _hb(client, "laptop", in_flight=[job_id])

    row = _row(client, job_id)
    assert row.state == "running", (
        f"a job orphaned as {reason}, reported IN FLIGHT by the very worker whose "
        "absence was the reason for orphaning it, stayed terminal. Its /complete will "
        "be discarded and hours of GPU work will be recorded as a death."
    )
    assert row.termination_reason is None, "the stale death reason must be cleared"
    assert row.finished_at is None, "a running job has not finished"


def test_the_completion_now_lands(client):
    """The point of the whole exercise: the result must be recorded, not discarded."""
    job_id = _claim(client, "laptop")
    _force(client, job_id, state="orphaned", termination_reason="worker_died")

    _hb(client, "laptop", in_flight=[job_id])
    r = client.post(
        f"/jobs/{job_id}/complete", json={"exit_code": 0}, headers={"X-Jobd-Worker": "laptop"}
    )

    assert r.status_code == 200, r.text
    body = client.get(f"/jobs/{job_id}").json()
    assert body["state"] == "completed", "terminal-is-terminal ate the real outcome again"
    assert body["exit_code"] == 0


# --- what must NOT be resurrected -----------------------------------------


def test_a_user_cancel_is_never_undone(client):
    """A human decided. The worker learns via GET /jobs/{id}/signal and kills it.
    Resurrecting here would override a person.

    NB: cancelling a RUNNING job does not flip the row to cancelled on the spot —
    it raises `signal` and waits for the worker to actually kill the workload and
    confirm. So force the post-confirmation row, which is the state this guard has
    to hold against.
    """
    job_id = _claim(client, "laptop")
    r = client.post(f"/jobs/{job_id}/cancel")
    assert r.status_code == 200, r.text
    _force(client, job_id, state="cancelled")

    _hb(client, "laptop", in_flight=[job_id])

    assert _state(client, job_id) == "cancelled", "a user's cancel was overridden by a heartbeat"


@pytest.mark.parametrize("state", ["completed", "failed"])
def test_a_real_outcome_is_never_undone(client, state):
    """Only the two worker-is-gone dispositions are undoable. A recorded outcome
    is somebody's answer and must stick."""
    job_id = _claim(client, "laptop")
    _force(client, job_id, state=state, exit_code=0 if state == "completed" else 1)

    _hb(client, "laptop", in_flight=[job_id])

    assert _row(client, job_id).state == state, f"a {state} job was resurrected"


def test_an_orphan_with_another_reason_is_not_resurrected(client):
    """worker_shutdown means the worker DRAINED and killed the workload on purpose.
    Only worker_died / worker_restarted rest on the 'the worker is gone' premise."""
    job_id = _claim(client, "laptop")
    _force(client, job_id, state="orphaned", termination_reason="worker_shutdown")

    _hb(client, "laptop", in_flight=[job_id])

    assert _row(client, job_id).state == "orphaned"


def test_a_job_re_dispatched_elsewhere_is_not_resurrected(client):
    """If the job now belongs to another worker, resurrecting our copy would put
    TWO live copies in flight, racing to /complete."""
    job_id = _claim(client, "laptop")
    _force(client, job_id, state="orphaned", termination_reason="worker_died", worker="desktop")

    _hb(client, "laptop", in_flight=[job_id])

    assert _row(client, job_id).state == "orphaned", (
        "a stale worker resurrected a job that had been re-dispatched to another host"
    )


def test_an_unknown_id_is_ignored(client):
    _hb(client, "laptop", in_flight=[999999])  # must not raise


# --- the cascade must be undone too ---------------------------------------


def test_resurrecting_a_parent_restores_the_children_it_cancelled(client):
    """Without this the DAG is left broken: the parent runs to completion while the
    children cancelled *because it died* stay dead forever."""
    parent = _claim(client, "laptop")
    child = client.post("/submit", json={**_SUBMIT, "depends_on": [parent]}).json()["id"]

    # the orphaning, exactly as the sweeper leaves it
    _force(client, parent, state="orphaned", termination_reason="worker_died")
    _force(client, child, state="cancelled", warning=f"parent_failed: {parent} → orphaned")

    _hb(client, "laptop", in_flight=[parent])

    assert _row(client, parent).state == "running"
    crow = _row(client, child)
    assert crow.state == "queued", (
        "the parent was resurrected but its cascade-cancelled child stayed cancelled — "
        "the parent will complete and the child will never run"
    )
    assert crow.warning is None, "the stale parent_failed warning must be cleared"


def test_a_child_with_another_genuinely_failed_parent_stays_cancelled(client):
    """The guard. Restoring it would queue a job that still cannot ever run."""
    good = _claim(client, "laptop")
    bad = client.post("/submit", json=_SUBMIT).json()["id"]
    child = client.post("/submit", json={**_SUBMIT, "depends_on": [good, bad]}).json()["id"]

    _force(client, bad, state="failed", exit_code=1)  # a REAL failure, not a lost worker
    _force(client, good, state="orphaned", termination_reason="worker_died")
    _force(client, child, state="cancelled", warning=f"parent_failed: {good} → orphaned")

    _hb(client, "laptop", in_flight=[good])

    assert _row(client, good).state == "running"
    assert _row(client, child).state == "cancelled", (
        "a child was re-queued even though one of its parents really did fail — "
        "it can never satisfy its deps and will sit queued forever"
    )


def test_the_uncascade_is_transitive(client):
    """A<-B<-C. Resurrecting A must restore B *and* C, mirroring the cascade that
    cancelled them."""
    a = _claim(client, "laptop")
    b = client.post("/submit", json={**_SUBMIT, "depends_on": [a]}).json()["id"]
    c = client.post("/submit", json={**_SUBMIT, "depends_on": [b]}).json()["id"]

    _force(client, a, state="orphaned", termination_reason="worker_died")
    _force(client, b, state="cancelled", warning=f"parent_failed: {a} → orphaned")
    _force(client, c, state="cancelled", warning=f"parent_failed: {b} → cancelled")

    _hb(client, "laptop", in_flight=[a])

    assert _row(client, a).state == "running"
    assert _row(client, b).state == "queued"
    assert _row(client, c).state == "queued", (
        "the un-cascade stopped at one level, like the bug it mirrors"
    )


def test_a_child_cancelled_by_a_user_is_not_restored(client):
    """No parent_failed warning => a human cancelled it => leave it alone."""
    parent = _claim(client, "laptop")
    child = client.post("/submit", json={**_SUBMIT, "depends_on": [parent]}).json()["id"]
    client.post(f"/jobs/{child}/cancel")

    _force(client, parent, state="orphaned", termination_reason="worker_died")

    _hb(client, "laptop", in_flight=[parent])

    assert _row(client, parent).state == "running"
    assert _row(client, child).state == "cancelled", "a user's cancel was undone by a resurrect"


# --- the event trail ------------------------------------------------------


def test_it_emits_job_resurrected(client, tmp_path):
    job_id = _claim(client, "laptop")
    _force(client, job_id, state="orphaned", termination_reason="worker_died")
    _hb(client, "laptop", in_flight=[job_id])

    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    kinds = [e["event"] for e in events]
    assert "job_resurrected" in kinds, (
        f"a resurrect left no trace: {kinds}. If this fires often the dead-worker "
        "threshold is too tight, and nobody can see that without the event."
    )


def test_old_workers_that_do_not_report_reconcile_nothing(client):
    """A heartbeat without the field is not a claim of 'nothing in flight'."""
    job_id = _claim(client, "laptop")
    _force(client, job_id, state="orphaned", termination_reason="worker_died")

    _hb(client, "laptop")  # no in_flight_job_ids at all

    assert _row(client, job_id).state == "orphaned"
