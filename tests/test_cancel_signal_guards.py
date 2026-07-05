"""Audit 2026-07-05 regression tests: preempt-signal write guards (A1) and
cancel-honoring claim teardown (A2), plus the smaller correctness fixes from
the same batch (F-3 finished_at on cwd_unreachable, phase-2 emit-after-commit,
bounded /log reads, /events reserved-key 422).

A1 — `signal` writers race state transitions like state writers do: the
sweeper's auto-preempt and /preempt-blockers stamped `signal='preempt'` with
plain ORM writes, and the /next-job claim never cleared `signal`, so a stale
preempt could ride a requeue onto a fresh claim and kill it seconds in.

A2 — every requeue path cleared `signal` unconditionally (M1), which erased a
pending user CANCEL: the user was told "cancelling", the claim was torn down
(admission refusal / dead worker), and the job silently re-ran elsewhere.
Teardown now honors a pending cancel (job -> CANCELLED) and only clears
non-cancel signals.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from jobd import app as app_mod
from jobd.app import build_app
from jobd.broker import sweeper as sweeper_mod
from jobd.broker.constants import MAX_LOG_CHUNK_BYTES
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


def _submit(client: TestClient, **overrides) -> int:
    payload = {"cmd": ["true"], "cwd": "/tmp", "project": "project-a", **overrides}
    r = client.post("/submit", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _claim(client: TestClient, host: str) -> dict | None:
    r = client.post(
        "/next-job",
        json={
            "host": host,
            "free_vram_gb": 0,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 8,
            "idle_cpus": 4,
        },
    )
    return r.json()


def _db_update(where_id: int, **values) -> None:
    engine = app_mod._engine_for_testing()
    with engine.begin() as conn:
        conn.execute(update(Job).where(Job.id == where_id).values(**values))


# ---------------------------------------------------------------------------
# A1 — stale-signal guards
# ---------------------------------------------------------------------------


def test_claim_clears_stale_signal_on_queued_row(client):
    """A1: a leftover signal on a QUEUED row (stamped by any unguarded writer
    mid-requeue) must not survive the queued->assigned claim — the fresh run
    would honor it on the first /signal poll and die ~2s in."""
    _hb(client, "w1")
    job_id = _submit(client)
    _db_update(job_id, signal="preempt")

    got = _claim(client, "w1")
    assert got is not None and got["id"] == job_id
    assert client.get(f"/jobs/{job_id}/signal").json()["signal"] is None


def test_sweeper_auto_preempt_signal_write_is_cas_guarded(client, monkeypatch):
    """A1: the sweeper's auto-preempt must not stamp 'preempt' onto a candidate
    that left RUNNING between the candidate SELECT and the signal write (a
    /complete or reconcile-requeue landing in the window). Injected exactly
    like the F1 reclaim test: flip the row terminal right as the guard runs,
    then delegate to the real CAS — which must match 0 rows and emit nothing."""
    _hb(client, "w1")
    candidate_id = _submit(client)
    assert _claim(client, "w1")["id"] == candidate_id
    client.post(f"/jobs/{candidate_id}/started")
    # Preemptible, low priority, running long enough to clear the runtime floor.
    _db_update(
        candidate_id,
        preemptible=True,
        priority=10,
        started_at=datetime.now(UTC) - timedelta(hours=1),
    )
    # High-priority queued job, old enough to trip the blocked-queue probe.
    queued_id = _submit(client)
    _db_update(queued_id, priority=90, submitted_at=datetime.now(UTC) - timedelta(hours=1))

    real_cas = sweeper_mod._cas_state

    def racing_cas(session, jid, expected, **values):
        if values.get("signal") == "preempt":
            monkeypatch.setattr(sweeper_mod, "_cas_state", real_cas)  # fire once
            session.execute(
                update(Job).where(Job.id == jid).values(state=JobState.COMPLETED.value, exit_code=0)
            )
        return real_cas(session, jid, expected, **values)

    monkeypatch.setattr(sweeper_mod, "_cas_state", racing_cas)
    app_mod._sweep_once()

    got = client.get(f"/jobs/{candidate_id}").json()
    assert got["state"] == "completed"
    assert client.get(f"/jobs/{candidate_id}/signal").json()["signal"] is None
    assert client.get("/events", params={"event": "auto_preempt"}).json() == []


def test_sweeper_auto_preempt_still_fires_on_live_candidate(client):
    """Companion to the race test: with no interference the auto-preempt CAS
    wins, the signal lands, and the event is emitted (after commit)."""
    _hb(client, "w1")
    candidate_id = _submit(client)
    assert _claim(client, "w1")["id"] == candidate_id
    client.post(f"/jobs/{candidate_id}/started")
    _db_update(
        candidate_id,
        preemptible=True,
        priority=10,
        started_at=datetime.now(UTC) - timedelta(hours=1),
    )
    queued_id = _submit(client)
    _db_update(queued_id, priority=90, submitted_at=datetime.now(UTC) - timedelta(hours=1))

    app_mod._sweep_once()

    assert client.get(f"/jobs/{candidate_id}/signal").json()["signal"] == "preempt"
    events = client.get("/events", params={"event": "auto_preempt"}).json()
    assert [e["job_id"] for e in events] == [candidate_id]


def test_preempt_blockers_lost_race_returns_unsignaled(client, monkeypatch):
    """A1: /preempt-blockers must not stamp 'preempt' onto a candidate that
    completed between its SELECT and the write; the caller gets signaled=None
    instead of a false success."""
    _hb(client, "w1")
    candidate_id = _submit(client)
    assert _claim(client, "w1")["id"] == candidate_id
    client.post(f"/jobs/{candidate_id}/started")
    _db_update(candidate_id, preemptible=True, priority=10)
    queued_id = _submit(client)
    _db_update(queued_id, priority=90)

    real_cas = app_mod._cas_state

    def racing_cas(session, jid, expected, **values):
        if values.get("signal") == "preempt":
            monkeypatch.setattr(app_mod, "_cas_state", real_cas)  # fire once
            session.execute(
                update(Job).where(Job.id == jid).values(state=JobState.COMPLETED.value, exit_code=0)
            )
        return real_cas(session, jid, expected, **values)

    monkeypatch.setattr(app_mod, "_cas_state", racing_cas)
    r = client.post(f"/jobs/{queued_id}/preempt-blockers", params={"force": True})
    assert r.status_code == 200
    assert r.json()["signaled"] is None

    assert client.get(f"/jobs/{candidate_id}").json()["state"] == "completed"
    assert client.get(f"/jobs/{candidate_id}/signal").json()["signal"] is None
    assert client.get("/events", params={"event": "auto_preempt"}).json() == []


# ---------------------------------------------------------------------------
# A2 — pending cancel honored on claim teardown
# ---------------------------------------------------------------------------


def _assigned_job_with_pending_cancel(client, host: str = "w1") -> int:
    _hb(client, host)
    job_id = _submit(client)
    assert _claim(client, host)["id"] == job_id
    r = client.post(f"/jobs/{job_id}/cancel")
    assert r.status_code == 200
    assert client.get(f"/jobs/{job_id}/signal").json()["signal"] == "cancel"
    return job_id


def test_refuse_admission_honors_pending_cancel_gpu_contention(client):
    """A2: user cancels an ASSIGNED job (told "cancelling"), then the worker
    refuses admission. The old requeue erased the cancel and the job re-ran
    elsewhere; it must land CANCELLED instead."""
    job_id = _assigned_job_with_pending_cancel(client)

    r = client.post(
        f"/jobs/{job_id}/refuse-admission",
        json={"reason": "gpu_contention", "required_gb": 8, "free_gb": 1},
    )
    assert r.status_code == 200, r.text
    got = client.get(f"/jobs/{job_id}").json()
    assert got["state"] == "cancelled", got
    assert got["finished_at"] is not None
    assert client.get(f"/jobs/{job_id}/signal").json()["signal"] is None
    # The honored cancel is on the event record; no admission_blocked requeue.
    cancels = client.get("/events", params={"event": "job_cancelled"}).json()
    assert any(
        e["job_id"] == job_id and e["payload"].get("via") == "refuse_admission" for e in cancels
    )
    # And the job must never be claimable again.
    assert _claim(client, "w1") is None


def test_refuse_admission_honors_pending_cancel_cwd_missing(client):
    """A2, cwd branch: same contract when the refusal reason is a missing cwd.
    Notably this must hold when NO other eligible worker has the cwd — without
    the hoisted cancel check the terminal branch mislabels the cancelled job
    as failed/cwd_unreachable. The cancel pre-empts the whole refusal
    disposition, so no cwd_refused re-route decision is recorded."""
    job_id = _assigned_job_with_pending_cancel(client)

    r = client.post(
        f"/jobs/{job_id}/refuse-admission",
        json={"reason": "cwd_missing", "cwd": "/tmp"},
    )
    assert r.status_code == 200, r.text
    got = client.get(f"/jobs/{job_id}").json()
    assert got["state"] == "cancelled", got
    assert got["termination_reason"] != "cwd_unreachable"
    assert client.get(f"/jobs/{job_id}/signal").json()["signal"] is None


def test_reconcile_requeue_honors_pending_cancel(client):
    """A2, reconcile path: a restarted worker (empty in-flight reports) whose
    ASSIGNED job has a pending cancel must see it CANCELLED, not requeued."""
    job_id = _assigned_job_with_pending_cancel(client, host="ghost")
    # Old enough to reconcile, and missing from 2 consecutive reports.
    _db_update(job_id, started_at=datetime.now(UTC) - timedelta(minutes=5))
    _hb(client, "ghost", in_flight=[])
    _hb(client, "ghost", in_flight=[])

    got = client.get(f"/jobs/{job_id}").json()
    assert got["state"] == "cancelled", got
    assert got["finished_at"] is not None
    cancels = client.get("/events", params={"event": "job_cancelled"}).json()
    assert any(e["job_id"] == job_id and e["payload"].get("via") == "reconcile" for e in cancels)


def test_reconcile_requeue_still_clears_preempt_signal(client):
    """M1 stays intact for non-cancel signals: a pending preempt on a dead
    worker's ASSIGNED job is cleared by the requeue, not honored."""
    _hb(client, "ghost")
    job_id = _submit(client)
    assert _claim(client, "ghost")["id"] == job_id
    _db_update(job_id, signal="preempt", started_at=datetime.now(UTC) - timedelta(minutes=5))
    _hb(client, "ghost", in_flight=[])
    _hb(client, "ghost", in_flight=[])

    got = client.get(f"/jobs/{job_id}").json()
    assert got["state"] == "queued", got
    assert client.get(f"/jobs/{job_id}/signal").json()["signal"] is None


# ---------------------------------------------------------------------------
# F-3 — cwd_unreachable is a fully-formed terminal
# ---------------------------------------------------------------------------


def test_cwd_unreachable_sets_finished_at(client):
    """F-3: the cwd_unreachable terminal transition must stamp finished_at like
    every other terminalizing write — retention pruning filters on it, so a
    NULL finished_at made these rows immortal."""
    _hb(client, "w1")
    job_id = _submit(client)
    assert _claim(client, "w1")["id"] == job_id

    r = client.post(
        f"/jobs/{job_id}/refuse-admission",
        json={"reason": "cwd_missing", "cwd": "/tmp"},
    )
    assert r.status_code == 200, r.text
    got = client.get(f"/jobs/{job_id}").json()
    assert got["state"] == "failed"
    assert got["termination_reason"] == "cwd_unreachable"
    assert got["finished_at"] is not None


# ---------------------------------------------------------------------------
# F-4 — phase-2 emit-after-commit
# ---------------------------------------------------------------------------


def test_sweeper_phase2_emits_nothing_when_commit_fails(client, tmp_path):
    """F-4: auto_preempt (and sweep_warning) events are emitted only after the
    phase-2 commit. A commit failure must not leave events.jsonl claiming a
    preempt signal the DB never recorded."""
    _hb(client, "w1")
    candidate_id = _submit(client)
    assert _claim(client, "w1")["id"] == candidate_id
    client.post(f"/jobs/{candidate_id}/started")
    _db_update(
        candidate_id,
        preemptible=True,
        priority=10,
        started_at=datetime.now(UTC) - timedelta(hours=1),
    )
    queued_id = _submit(client)
    _db_update(queued_id, priority=90, submitted_at=datetime.now(UTC) - timedelta(hours=1))

    engine = app_mod._engine_for_testing()

    class ExplodingSecondCommit(Session):
        commits = 0

        def commit(self):
            type(self).commits += 1
            if type(self).commits >= 2:  # 1st = phase 1, 2nd = phase 2
                raise RuntimeError("injected phase-2 commit failure")
            super().commit()

    factory = sessionmaker(bind=engine, class_=ExplodingSecondCommit)
    logs_dir = tmp_path / "logs"  # same logs_path the client fixture built with

    with pytest.raises(RuntimeError, match="injected phase-2 commit failure"):
        sweeper_mod.sweep_once(factory, logs_dir, lambda: None)

    # The signal write was rolled back with the commit — and no event claims it.
    assert client.get(f"/jobs/{candidate_id}/signal").json()["signal"] is None
    assert client.get("/events", params={"event": "auto_preempt"}).json() == []


# ---------------------------------------------------------------------------
# /log bounded read + /events reserved keys
# ---------------------------------------------------------------------------


def test_log_append_rejects_oversized_chunk(client):
    """The /log size gate must fire on an oversized chunk (and the read is
    bounded — the old code buffered the whole body before checking)."""
    _hb(client, "w1")
    job_id = _submit(client)
    assert _claim(client, "w1")["id"] == job_id

    r = client.post(f"/jobs/{job_id}/log", content=b"x" * (MAX_LOG_CHUNK_BYTES + 1))
    assert r.status_code == 413

    ok = client.post(f"/jobs/{job_id}/log", content=b"hello\n")
    assert ok.status_code == 200
    assert ok.json()["bytes"] == 6


def test_events_payload_reserved_key_is_422_not_500(client):
    """A payload key colliding with an envelope field ('source', 'job_id', …)
    used to raise TypeError at the _emit_event call — an uncaught 500. It must
    be a 422 at the Pydantic boundary."""
    r = client.post(
        "/events",
        json={"source": "hook", "event": "custom", "payload": {"source": "broker"}},
    )
    assert r.status_code == 422
    assert "reserved" in r.text

    ok = client.post(
        "/events",
        json={"source": "hook", "event": "custom", "payload": {"note": "fine"}},
    )
    assert ok.status_code == 204
