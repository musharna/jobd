"""The sweeper must not reclaim across an interval it did not observe.

Every reclaim decision is a wall-clock delta against a stored timestamp — "this
worker last heartbeat 400s ago, so it is dead". That inference is only valid if
the broker was WATCHING those 400s, and it has two ways not to be: it just
started (the first sweep runs immediately at boot, before any worker can
re-heartbeat), or it was frozen (host suspend, loop stall) so `now` jumped while
nothing was observed.

Without the guard the first post-gap sweep sees the whole fleet as dead and
every in-flight job as reclaimable, and acts on all of it at once — including
the `wall_clock_exceeded` branch, whose terminal resurrect does NOT undo. Audit
2026-07-25 H-1.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select

from jobd.broker import sweeper
from jobd.broker.constants import DEAD_WORKER_SECONDS
from jobd.db import Job, Worker
from jobd.models import JobState


@pytest.fixture(autouse=True)
def _fresh_observation_state():
    sweeper.reset_observation_state()
    yield
    sweeper.reset_observation_state()


def _age_the_broker() -> None:
    """Pretend the process has been up a long time, so uptime is not the reason
    reclaim is suppressed and a test can isolate the GAP reason."""
    sweeper._process_start_monotonic = time.monotonic() - (DEAD_WORKER_SECONDS * 10)


def _last_sweep_was(seconds_ago: float) -> None:
    sweeper._last_sweep_wall = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        seconds=seconds_ago
    )


def _seed(
    client,
    *,
    silent_for_s: int,
    max_wall_s: int | None = None,
    started_ago_s: int | None = None,
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    started_ago_s = silent_for_s + 90 if started_ago_s is None else started_ago_s
    with client.app.state.engine.begin() as conn:
        conn.execute(
            insert(Worker).values(
                host="ghost",
                state="online",
                last_heartbeat=now - timedelta(seconds=silent_for_s),
            )
        )
        conn.execute(
            insert(Job).values(
                project="p",
                priority=50,
                cmd_json='["sleep","1"]',
                cwd="/tmp",
                state=JobState.RUNNING.value,
                worker="ghost",
                submitted_at=now - timedelta(seconds=started_ago_s + 30),
                started_at=now - timedelta(seconds=started_ago_s),
                max_wall_s=max_wall_s,
            )
        )


def _job(client):
    with client.app.state.engine.begin() as conn:
        return conn.execute(select(Job.state, Job.termination_reason)).one()


def test_first_sweep_after_start_does_not_reclaim(client):
    """Trigger 1: restart. The stored heartbeat is old purely because nobody was
    running to receive a newer one."""
    _seed(client, silent_for_s=DEAD_WORKER_SECONDS * 3)

    client.app.state.sweep_once()

    assert _job(client).state == JobState.RUNNING.value, (
        "the first sweep after start reclaimed a job on the strength of an interval "
        "it never observed"
    )


def test_sweep_after_an_unobserved_gap_does_not_reclaim(client):
    """Trigger 2: suspend/stall. Uptime is fine; the GAP is the problem."""
    _age_the_broker()
    _last_sweep_was(8 * 3600)
    _seed(client, silent_for_s=DEAD_WORKER_SECONDS * 3)

    client.app.state.sweep_once()

    assert _job(client).state == JobState.RUNNING.value


def test_a_normally_paced_sweep_still_reclaims(client):
    """The control. The guard must not disable reclaim in steady state — that
    would trade one silent failure for a worse one."""
    _age_the_broker()
    _last_sweep_was(30)
    _seed(client, silent_for_s=DEAD_WORKER_SECONDS * 3)

    client.app.state.sweep_once()

    assert _job(client).state == JobState.ORPHANED.value, (
        "a steady-state sweep stopped reclaiming from a genuinely dead worker"
    )


def test_the_unrecoverable_wall_clock_terminal_is_suppressed_too(client):
    """The sharpest case: `wall_clock_exceeded` is a DELIBERATE terminal that
    resurrect will not undo, so a false positive here is PERMANENT — unlike a
    worker_died orphan, which a returning worker reverses."""
    _age_the_broker()
    _last_sweep_was(8 * 3600)
    # Heartbeat is FRESH, so the dead-worker path cannot fire. The only thing
    # that could kill this job is the wall-clock backstop reading a started_at
    # from before the gap: 8h elapsed against max_wall_s=60 (+120s grace).
    _seed(client, silent_for_s=0, max_wall_s=60, started_ago_s=8 * 3600)

    client.app.state.sweep_once()

    row = _job(client)
    assert row.state == JobState.RUNNING.value, (
        f"a healthy job was terminated {row.termination_reason!r} on the first sweep after "
        "an 8h gap — and that terminal is not resurrectable"
    )


def test_suppression_is_announced_not_silent(client_logs, caplog):
    """A sweep that declines to reclaim must never be indistinguishable from a
    sweep that found nothing to reclaim."""
    client, logs_dir = client_logs
    _seed(client, silent_for_s=DEAD_WORKER_SECONDS * 3)

    with caplog.at_level("WARNING"):
        client.app.state.sweep_once()

    assert any("reclaim phases suppressed" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]
    events = [json.loads(ln) for ln in (logs_dir / "events.jsonl").read_text().splitlines() if ln]
    assert any(e["event"] == "reclaim_suppressed" for e in events), events


def test_the_guard_clears_on_the_following_sweep(client):
    """Suppression lasts ONE pass. Once a normal interval has been observed,
    reclaim resumes — otherwise a single gap would disable it forever."""
    _age_the_broker()
    _last_sweep_was(8 * 3600)
    _seed(client, silent_for_s=DEAD_WORKER_SECONDS * 3)

    client.app.state.sweep_once()
    assert _job(client).state == JobState.RUNNING.value

    client.app.state.sweep_once()  # the observed interval is now ~0s
    assert _job(client).state == JobState.ORPHANED.value
