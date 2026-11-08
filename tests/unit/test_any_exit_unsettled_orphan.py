"""An any-exit child must not start while its parent may still be running.

A parent orphaned as `worker_died`/`worker_restarted` is ORPHANED, which is in
TERMINAL_STATES, but it is the one terminal that can come back: if its worker was
frozen rather than dead, the next heartbeat resurrects it to RUNNING
(`_resurrect_reported_orphans`). A default-policy child copes, because it is
cascade-cancelled and restored by the un-cascade. An any-exit child had no such
path: "any terminal unblocks" let it dispatch on the orphan write, so a cleanup
step ran while its parent was still running.

Such an orphan is UNSETTLED until one of two things makes the resurrect
impossible in practice:
  - its worker heartbeats after the orphan was written and does not report the
    job (the same transaction would have resurrected it), or
  - JOBD_ORPHAN_SETTLE_S has passed since the orphan (a worker that never returns).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from jobd.broker import state as state_mod
from jobd.db import Job, Worker

_SUBMIT = {"cmd": ["bash", "-c", "sleep 60"], "cwd": "/tmp", "project": "project-a"}


def _hb(client: TestClient, host: str, in_flight: list[int] | None = None) -> None:
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


def _claim(client: TestClient, host: str) -> int:
    job_id = client.post("/submit", json=_SUBMIT).json()["id"]
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


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _orphan(client: TestClient, job_id: int, reason: str, *, age_s: float = 0) -> None:
    """Leave the parent exactly as the sweeper does, with its worker gone quiet."""
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                state="orphaned",
                termination_reason=reason,
                finished_at=_now() - timedelta(seconds=age_s),
            )
        )
        conn.execute(
            update(Worker)
            .where(Worker.host == "laptop")
            .values(last_heartbeat=_now() - timedelta(seconds=age_s + 120))
        )


def _child(client: TestClient, parent: int, *, any_exit: bool) -> int:
    body = {**_SUBMIT, "depends_on": [parent], "depends_on_any_exit": any_exit}
    r = client.post("/submit", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _satisfied(client: TestClient, job_id: int) -> bool:
    with Session(client.app.state.engine) as s:
        return state_mod._deps_satisfied(s.get(Job, job_id), s)


def test_an_any_exit_child_waits_on_a_parent_that_may_still_be_running(client):
    # Both parents claimed before either is orphaned: a claim heartbeats, and a
    # heartbeat after the orphan would (correctly) settle it.
    parent = _claim(client, "laptop")
    other = _claim(client, "laptop")
    child = _child(client, parent, any_exit=True)
    other_child = _child(client, other, any_exit=True)
    _orphan(client, parent, "worker_died")
    _orphan(client, other, "wall_clock_exceeded")

    assert not _satisfied(client, child), (
        "an any-exit child was released by a worker_died orphan whose worker may only "
        "be frozen; a resurrect would leave the child running beside a live parent"
    )
    # Positive control, same harness: a terminal that cannot come back releases it.
    assert _satisfied(client, other_child)


def test_a_worker_restarted_orphan_is_unsettled_too(client):
    parent = _claim(client, "laptop")
    child = _child(client, parent, any_exit=True)
    _orphan(client, parent, "worker_restarted")
    assert not _satisfied(client, child)


def test_a_heartbeat_that_does_not_report_the_job_settles_it(client):
    parent = _claim(client, "laptop")
    child = _child(client, parent, any_exit=True)
    _orphan(client, parent, "worker_died")
    assert not _satisfied(client, child)

    _hb(client, "laptop", in_flight=[])  # back, and not running it

    assert client.get(f"/jobs/{parent}").json()["state"] == "orphaned"
    assert _satisfied(client, child), "the worker came back without the job; it is dead"


def test_a_heartbeat_that_reports_the_job_resurrects_it_and_the_child_keeps_waiting(client):
    parent = _claim(client, "laptop")
    child = _child(client, parent, any_exit=True)
    _orphan(client, parent, "worker_died")

    _hb(client, "laptop", in_flight=[parent])

    assert client.get(f"/jobs/{parent}").json()["state"] == "running"
    assert not _satisfied(client, child)


def test_an_orphan_whose_worker_never_returns_settles_after_the_cap(client, monkeypatch):
    monkeypatch.setenv("JOBD_ORPHAN_SETTLE_S", "600")
    parent = _claim(client, "laptop")
    child = _child(client, parent, any_exit=True)

    _orphan(client, parent, "worker_died", age_s=300)
    assert not _satisfied(client, child)

    _orphan(client, parent, "worker_died", age_s=700)
    assert _satisfied(client, child), "a worker that never returns must not strand the child"


def test_the_default_policy_is_unchanged(client):
    """A default-policy child still never runs on an orphaned parent (it is
    cascade-cancelled, and restored if the parent comes back)."""
    parent = _claim(client, "laptop")
    child = _child(client, parent, any_exit=False)
    _orphan(client, parent, "worker_died", age_s=10_000)
    assert not _satisfied(client, child)
