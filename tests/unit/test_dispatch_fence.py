"""A stale report from an earlier claim of the same job is refused.

`_reject_stale_worker` fenced reports by HOSTNAME only. When a job is requeued
(sweeper/reconcile, e.g. after a WSL freeze) and the SAME host claims it again,
the original run's /log, /started and /complete carry the same X-Jobd-Worker
and were accepted into the new run: a stale /complete terminal-ized the live
re-run (double execution, wrong outcome recorded).

Every claim now bumps `dispatch_seq`; the worker echoes the seq it was handed in
X-Jobd-Dispatch, and the broker refuses a mismatch. A resurrect is not a claim,
so a revived orphan's own reports keep matching. No header = an older worker:
the hostname check alone applies, as before.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import update

from jobd.db import Job

_SUBMIT = {"cmd": ["bash", "-c", "sleep 60"], "cwd": "/tmp", "project": "project-a"}
_HB = {"free_vram_gb": 0, "unregistered_vram_gb": 0, "free_ram_gb": 8, "idle_cpus": 4}
_ASK = {"free_vram_gb": 48, "unregistered_vram_gb": 0, "free_ram_gb": 64, "idle_cpus": 16}


def _claim(client: TestClient, host: str) -> dict:
    client.post("/heartbeat", json={"host": host, **_HB})
    r = client.post("/next-job", json={"host": host, **_ASK})
    assert r.status_code == 200 and r.json(), r.text
    return r.json()


def _requeue(client: TestClient, job_id: int) -> None:
    """What the reconcile does to an idempotent job it thinks is gone."""
    with client.app.state.engine.begin() as conn:
        conn.execute(
            update(Job).where(Job.id == job_id).values(state="queued", worker=None, started_at=None)
        )


def _hdr(host: str, seq: int | None) -> dict:
    h = {"X-Jobd-Worker": host}
    if seq is not None:
        h["X-Jobd-Dispatch"] = str(seq)
    return h


def test_each_claim_hands_out_a_new_dispatch_seq(client):
    job_id = client.post("/submit", json=_SUBMIT).json()["id"]
    first = _claim(client, "laptop")
    _requeue(client, job_id)
    second = _claim(client, "laptop")
    assert first["id"] == second["id"] == job_id
    assert second["dispatch_seq"] == first["dispatch_seq"] + 1


def test_a_stale_completion_from_the_same_host_is_refused(client):
    job_id = client.post("/submit", json=_SUBMIT).json()["id"]
    old = _claim(client, "laptop")["dispatch_seq"]
    _requeue(client, job_id)
    new = _claim(client, "laptop")["dispatch_seq"]
    assert client.post(f"/jobs/{job_id}/started", headers=_hdr("laptop", new)).status_code == 200

    stale = client.post(
        f"/jobs/{job_id}/complete", json={"exit_code": 1}, headers=_hdr("laptop", old)
    )
    assert stale.status_code == 409, (
        "a /complete from the run that was requeued was accepted into the re-run "
        f"on the same host: {stale.status_code} {stale.text}"
    )
    assert client.get(f"/jobs/{job_id}").json()["state"] == "running"

    # Positive control, same harness: the current run's report lands.
    ok = client.post(f"/jobs/{job_id}/complete", json={"exit_code": 0}, headers=_hdr("laptop", new))
    assert ok.status_code == 200, ok.text
    assert client.get(f"/jobs/{job_id}").json()["state"] == "completed"


def test_a_stale_log_and_started_are_refused_too(client):
    job_id = client.post("/submit", json=_SUBMIT).json()["id"]
    old = _claim(client, "laptop")["dispatch_seq"]
    _requeue(client, job_id)
    _claim(client, "laptop")

    assert client.post(f"/jobs/{job_id}/started", headers=_hdr("laptop", old)).status_code == 409
    log = client.post(f"/jobs/{job_id}/log", content=b"stale\n", headers=_hdr("laptop", old))
    assert log.status_code == 409


def test_a_worker_that_sends_no_dispatch_header_is_still_accepted(client):
    """Backward compatible: an older worker binary sends only X-Jobd-Worker."""
    job_id = client.post("/submit", json=_SUBMIT).json()["id"]
    _claim(client, "laptop")
    assert client.post(f"/jobs/{job_id}/started", headers=_hdr("laptop", None)).status_code == 200
    r = client.post(f"/jobs/{job_id}/complete", json={"exit_code": 0}, headers=_hdr("laptop", None))
    assert r.status_code == 200, r.text


def test_a_resurrected_orphan_keeps_its_dispatch_seq(client):
    """A resurrect is the same run coming back, not a new claim."""
    job_id = client.post("/submit", json=_SUBMIT).json()["id"]
    seq = _claim(client, "laptop")["dispatch_seq"]
    client.post(f"/jobs/{job_id}/started", headers=_hdr("laptop", seq))
    with client.app.state.engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(state="orphaned", termination_reason="worker_died")
        )
    client.post("/heartbeat", json={"host": "laptop", **_HB, "in_flight_job_ids": [job_id]})
    assert client.get(f"/jobs/{job_id}").json()["state"] == "running"

    r = client.post(f"/jobs/{job_id}/complete", json={"exit_code": 0}, headers=_hdr("laptop", seq))
    assert r.status_code == 200, r.text
