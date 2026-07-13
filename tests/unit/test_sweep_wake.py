"""The sweeper must wake long-polling dispatchers only when it CHANGED something.

`sweep_once` used to call `wake_dispatchers()` on every pass, i.e. unconditionally
every SWEEP_INTERVAL_SECONDS. That wake is a broadcast: every parked `/next-job`
long-poll wakes at once, and each then runs a full claim attempt (queue scan +
dependency reads + fit walk) of which at most one can succeed. The overwhelmingly
common sweep changes nothing at all, and a sweep that changed no state cannot have
made any job newly dispatchable — so there was nothing to wake for.

The safety argument for waking less: a missed wake is not a stall. Parked workers
re-attempt every `_LONGPOLL_RECHECK_S` (10s) regardless, exactly as a backstop for
wake sites that don't fire. So if the "did anything change?" predicate is ever too
narrow, the cost is bounded at ~10s of extra queue latency, never a stuck job. These
tests pin both halves: silent when idle, and still waking on the transition that
actually makes a job dispatchable again (a dead worker's job being requeued).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from jobd.db import Worker


@pytest.fixture
def wake_counter(client, monkeypatch):
    """Count wake_dispatchers() calls made by the sweeper.

    app.state.sweep_once is the closure build_app() already bound to the real
    _wake_dispatchers, so re-bind sweep_once to a fresh closure over our counter.
    """
    import jobd.broker.sweeper as sweeper_mod

    calls: list[int] = []
    session_local = client.app.state.SessionLocal
    logs_dir = client.app.state.shared["logs_dir"]

    def sweep_with_counting_wake() -> None:
        sweeper_mod.sweep_once(session_local, logs_dir, lambda: calls.append(1))

    client.app.state.sweep_once = sweep_with_counting_wake
    return calls


def test_idle_sweep_does_not_wake_dispatchers(client, wake_counter):
    """A sweep that changes nothing must not broadcast a wake.

    This is the common case — it ran every 30s forever, waking every parked worker
    to re-scan a queue that had not changed.
    """
    client.app.state.sweep_once()
    assert wake_counter == [], (
        "an idle sweep woke the dispatchers; every parked long-poll just re-ran a "
        "full claim attempt for nothing"
    )


def test_sweep_still_wakes_when_a_dead_workers_job_is_requeued(client, wake_counter):
    """The transition that MUST still wake: a job returning to QUEUED.

    If this regresses, a reclaimed job sits until a worker's 10s backstop recheck
    instead of dispatching immediately — the exact latency the wake exists to avoid.
    """
    client.post(
        "/heartbeat",
        json={
            "host": "ghost",
            "free_vram_gb": 0,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 8,
            "idle_cpus": 4,
        },
    )
    job_id = client.post(
        "/submit",
        json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"},
    ).json()["id"]
    claim = client.post(
        "/next-job",
        json={
            "host": "ghost",
            "free_vram_gb": 0,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 8,
            "idle_cpus": 4,
        },
    )
    assert claim.json()["id"] == job_id

    # Kill the worker: backdate its heartbeat past the reclaim cutoff.
    with client.app.state.engine.begin() as conn:
        conn.execute(
            update(Worker)
            .where(Worker.host == "ghost")
            .values(last_heartbeat=datetime.now(UTC) - timedelta(minutes=6))
        )

    client.app.state.sweep_once()

    assert client.get(f"/jobs/{job_id}").json()["state"] == "queued", (
        "precondition: the sweep should have requeued the dead worker's job"
    )
    assert wake_counter, (
        "the sweep requeued a job (making it dispatchable) but did not wake the "
        "parked dispatchers — it will not be picked up until a backstop recheck"
    )
