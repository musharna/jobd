"""Opt-in per-job retry (`max_retries`).

The broker still never re-runs a failed job on its own: the default is 0 and
every test here that asserts a retry also asserts that the same report without
the opt-in stays failed. What is retried is exactly one thing -- the workload
exited non-zero with no termination reason. A timeout, a preempt, a cancel and a
worker-side fault each carry a reason or a different final state, and re-running
those would either repeat a verdict the broker already reached or hide a broken
host behind a second attempt.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

HB = {
    "free_vram_gb": 0,
    "unregistered_vram_gb": 0,
    "free_ram_gb": 8,
    "idle_cpus": 4,
}


def submit(client, **extra):
    r = client.post(
        "/submit", json={"cmd": ["false"], "cwd": "/tmp", "project": "project-a", **extra}
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def claim(client, host="w1"):
    client.post(
        "/heartbeat",
        json={
            "host": host,
            **HB,
            "arch": "x86_64",
            "os": "linux",
            "gpu": False,
            "tags": [],
            "host_aliases": [],
        },
    )
    body = client.post("/next-job", json={"host": host, **HB}).json()
    if body is not None:
        client.post(f"/jobs/{body['id']}/started", headers={"X-Jobd-Worker": host})
    return body


def complete(client, job_id, host="w1", **payload):
    r = client.post(f"/jobs/{job_id}/complete", json=payload, headers={"X-Jobd-Worker": host})
    assert r.status_code == 200, r.text
    return r.json()


def events(logs_dir, kind):
    path = logs_dir / "events.jsonl"
    rows = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
    return [r for r in rows if r.get("event") == kind]


def test_failed_exit_is_requeued_until_retries_run_out(client_logs):
    client, logs = client_logs
    jid = submit(client, max_retries=2)
    seen = []
    for expected_attempt in (0, 1, 2):
        job = claim(client)
        assert job is not None and job["id"] == jid
        assert job["attempt"] == expected_attempt
        seen.append(complete(client, jid, exit_code=3))
    assert [(j["state"], j["attempt"], j["exit_code"]) for j in seen] == [
        ("queued", 1, None),
        ("queued", 2, None),
        ("failed", 2, 3),
    ]
    # A requeued attempt is a fresh claim: no worker, no start stamp, no finish.
    assert (seen[0]["worker"], seen[0]["started_at"], seen[0]["finished_at"]) == (None, None, None)
    assert claim(client) is None
    retries = events(logs, "job_retry_scheduled")
    assert [(e["job_id"], e["payload"]) for e in retries] == [
        (jid, {"exit_code": 3, "attempt": n, "max_retries": 2, "retry_delay_s": 0, "worker": "w1"})
        for n in (1, 2)
    ]
    # One terminal event, for the attempt that actually ended the job.
    assert [e["payload"]["final_state"] for e in events(logs, "job_completed")] == ["failed"]


def test_without_the_opt_in_a_failed_job_stays_failed(client_logs):
    client, logs = client_logs
    jid = submit(client)
    claim(client)
    job = complete(client, jid, exit_code=3)
    assert (job["state"], job["attempt"], job["max_retries"]) == ("failed", 0, 0)
    assert events(logs, "job_retry_scheduled") == []


@pytest.mark.parametrize(
    "payload, final",
    [
        ({"exit_code": 0}, "completed"),
        (
            {
                "exit_code": 124,
                "final_state": "failed",
                "termination_reason": "wall_clock_exceeded",
            },
            "failed",
        ),
        (
            {"exit_code": 1, "final_state": "failed", "termination_reason": "launcher_missing"},
            "failed",
        ),
        ({"exit_code": -15, "final_state": "cancelled"}, "cancelled"),
        ({"exit_code": -15, "final_state": "preempted"}, "preempted"),
    ],
)
def test_only_a_plain_nonzero_exit_is_retried(client, payload, final):
    jid = submit(client, max_retries=5)
    claim(client)
    job = complete(client, jid, **payload)
    assert (job["state"], job["attempt"]) == (final, 0)
    # Control: same submission, plain failure -> the opt-in does fire.
    jid2 = submit(client, max_retries=5)
    assert claim(client)["id"] == jid2
    assert complete(client, jid2, exit_code=1)["state"] == "queued"


def test_retry_delay_holds_the_job_back(client, app):
    from jobd.db import Job

    jid = submit(client, max_retries=1, retry_delay_s=600)
    claim(client)
    before = datetime.now(UTC).replace(tzinfo=None)
    job = complete(client, jid, exit_code=1)
    assert job["state"] == "queued"
    not_before = datetime.fromisoformat(job["not_before"]).replace(tzinfo=None)
    assert timedelta(seconds=595) < not_before - before < timedelta(seconds=605)
    assert claim(client) is None  # still inside the delay
    # Once the delay has passed the same job is offered again.
    with app.state.SessionLocal() as s:
        row = s.get(Job, jid)
        row.not_before = datetime.now(UTC) - timedelta(seconds=1)
        s.commit()
    again = claim(client)
    # The hold is spent once the job is claimed; a finished job must not go on
    # advertising a "next attempt" (seen on a real broker run, not in a unit test).
    assert (again["id"], again["not_before"]) == (jid, None)


def test_cancel_pending_at_failure_wins_over_the_retry(client_logs):
    client, logs = client_logs
    jid = submit(client, max_retries=3)
    claim(client)
    assert client.post(f"/jobs/{jid}/cancel").status_code == 200  # running -> signal=cancel
    job = complete(client, jid, exit_code=1)  # worker reports a plain failure first
    # The user was told "cancelling": whatever the terminal label, it must not run again.
    assert (job["state"], job["attempt"]) == ("failed", 0)
    assert events(logs, "job_retry_scheduled") == []
    assert claim(client) is None


def test_children_wait_for_the_retry_instead_of_being_cancelled(client):
    parent = submit(client, max_retries=1)
    child = client.post(
        "/submit",
        json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a", "depends_on": [parent]},
    ).json()["id"]
    claim(client)
    complete(client, parent, exit_code=1)
    assert client.get(f"/jobs/{child}").json()["state"] == "queued"
    assert claim(client)["id"] == parent  # the child is still blocked on it
    complete(client, parent, exit_code=1)  # retries exhausted -> terminal -> cascade
    assert client.get(f"/jobs/{parent}").json()["state"] == "failed"
    assert client.get(f"/jobs/{child}").json()["state"] == "cancelled"


@pytest.mark.parametrize("bad", [{"max_retries": -1}, {"max_retries": 21}, {"retry_delay_s": -1}])
def test_out_of_range_retry_options_are_rejected(client, bad):
    r = client.post("/submit", json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a", **bad})
    assert r.status_code == 422


def test_a_job_row_from_before_the_upgrade_is_never_retried(tmp_path):
    """The live database is upgraded in place by ALTER TABLE. SQLite back-fills an
    added column with its DEFAULT, and a NULL that slipped through must still read
    as "no retries" rather than crash the /complete handler or the matcher."""
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.orm import sessionmaker

    from jobd.broker.jobinfo import _to_info
    from jobd.db import Job, init_db, migrate

    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = create_engine(url)
    init_db(engine)
    with sessionmaker(engine)() as s:
        s.add(
            Job(
                project="project-a",
                priority=50,
                state="running",
                cmd_json='["false"]',
                cwd="/tmp",
                submitted_at=datetime(2026, 1, 1),
            )
        )
        s.commit()
    with engine.begin() as conn:  # back to the pre-upgrade table shape
        for col in ("max_retries", "retry_delay_s", "attempt", "not_before"):
            conn.execute(text(f"ALTER TABLE jobs DROP COLUMN {col}"))
    assert "attempt" not in {c["name"] for c in inspect(engine).get_columns("jobs")}
    migrate(engine)
    with engine.begin() as conn:  # the worst case: the default did not take
        conn.execute(
            text("UPDATE jobs SET max_retries = NULL, attempt = NULL, retry_delay_s = NULL")
        )
    with sessionmaker(engine)() as s:
        info = _to_info(s.get(Job, 1))
    assert (info.attempt, info.max_retries, info.retry_delay_s, info.not_before) == (0, 0, 0, None)
