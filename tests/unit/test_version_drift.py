"""version_drift sweep event (backlog 2026-07-15): an ONLINE worker whose
self-reported version mismatches the broker's continuously past
JOBD_VERSION_DRIFT_WARN_HOURS gets ONE event per episode.

Short-lived mismatch is normal (the worker CD defers while a job runs); the
event exists for the episode that never self-heals — the 12-day cwd_missing
incident (workers silently on 0.5.3 while the fix shipped in 0.5.10) is the
canonical cost of not noticing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import insert, select, update

from jobd import __version__ as BROKER_VERSION
from jobd.db import Worker


def _add_worker(client, host: str, *, version: str | None, state: str = "online") -> None:
    with client.app.state.engine.begin() as conn:
        conn.execute(
            insert(Worker).values(
                host=host,
                last_heartbeat=datetime.now(UTC).replace(tzinfo=None),
                state=state,
                version=version,
            )
        )


def _stamps(client, host: str):
    with client.app.state.engine.begin() as conn:
        return conn.execute(
            select(Worker.version_mismatch_since, Worker.version_drift_warned_at).where(
                Worker.host == host
            )
        ).one()


def _drift_events(logs_dir) -> list[dict]:
    f = logs_dir / "events.jsonl"
    if not f.exists():
        return []
    rows = [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("event") == "version_drift"]


def test_warns_once_per_episode_after_threshold(client_logs, monkeypatch):
    monkeypatch.delenv("JOBD_VERSION_DRIFT_WARN_HOURS", raising=False)
    client, logs = client_logs
    _add_worker(client, "stale-box", version="0.0.1")
    t0 = datetime.now(UTC).replace(tzinfo=None)

    # First observation only stamps the episode start — no event.
    assert client.app.state.warn_version_drift(t0) == 0
    since, warned = _stamps(client, "stale-box")
    assert since is not None and warned is None
    assert _drift_events(logs) == []

    # Under the 24h default: still quiet (a busy worker deferring is normal).
    assert client.app.state.warn_version_drift(t0 + timedelta(hours=23)) == 0

    # Past it: exactly one event, with the versions and duration in the payload.
    assert client.app.state.warn_version_drift(t0 + timedelta(hours=25)) == 1
    events = _drift_events(logs)
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["host"] == "stale-box"
    assert payload["worker_version"] == "0.0.1"
    assert payload["broker_version"] == BROKER_VERSION
    assert payload["mismatch_hours"] >= 25.0

    # Dedup: the same episode never re-warns.
    assert client.app.state.warn_version_drift(t0 + timedelta(hours=100)) == 0
    assert len(_drift_events(logs)) == 1


def test_alignment_clears_episode_and_a_new_one_rewarns(client_logs, monkeypatch):
    monkeypatch.delenv("JOBD_VERSION_DRIFT_WARN_HOURS", raising=False)
    client, logs = client_logs
    _add_worker(client, "flappy", version="0.0.1")
    t0 = datetime.now(UTC).replace(tzinfo=None)
    client.app.state.warn_version_drift(t0)
    client.app.state.warn_version_drift(t0 + timedelta(hours=25))
    assert len(_drift_events(logs)) == 1

    # The worker updates: stamps clear.
    with client.app.state.engine.begin() as conn:
        conn.execute(update(Worker).where(Worker.host == "flappy").values(version=BROKER_VERSION))
    client.app.state.warn_version_drift(t0 + timedelta(hours=26))
    assert _stamps(client, "flappy") == (None, None)

    # A later episode (next release) warns afresh.
    with client.app.state.engine.begin() as conn:
        conn.execute(update(Worker).where(Worker.host == "flappy").values(version="0.0.2"))
    t1 = t0 + timedelta(hours=30)
    client.app.state.warn_version_drift(t1)
    assert client.app.state.warn_version_drift(t1 + timedelta(hours=25)) == 1
    assert len(_drift_events(logs)) == 2


def test_versionless_worker_counts_as_drift(client_logs, monkeypatch):
    """A worker too old to report a version is running old code by definition."""
    monkeypatch.delenv("JOBD_VERSION_DRIFT_WARN_HOURS", raising=False)
    client, logs = client_logs
    _add_worker(client, "ancient", version=None)
    t0 = datetime.now(UTC).replace(tzinfo=None)
    client.app.state.warn_version_drift(t0)
    assert client.app.state.warn_version_drift(t0 + timedelta(hours=25)) == 1
    assert _drift_events(logs)[0]["payload"]["worker_version"] is None


def test_offline_worker_and_current_worker_never_stamped(client_logs, monkeypatch):
    """Offline workers drift trivially (the offline warning covers them);
    a current worker must never even start an episode."""
    monkeypatch.delenv("JOBD_VERSION_DRIFT_WARN_HOURS", raising=False)
    client, logs = client_logs
    _add_worker(client, "gone", version="0.0.1", state="offline")
    _add_worker(client, "fresh", version=BROKER_VERSION)
    t0 = datetime.now(UTC).replace(tzinfo=None)
    client.app.state.warn_version_drift(t0)
    assert client.app.state.warn_version_drift(t0 + timedelta(hours=48)) == 0
    assert _stamps(client, "gone") == (None, None)
    assert _stamps(client, "fresh") == (None, None)
    assert _drift_events(logs) == []


def test_negative_setting_disables(client_logs, monkeypatch):
    monkeypatch.setenv("JOBD_VERSION_DRIFT_WARN_HOURS", "-1")
    client, logs = client_logs
    _add_worker(client, "stale-box", version="0.0.1")
    t0 = datetime.now(UTC).replace(tzinfo=None)
    client.app.state.warn_version_drift(t0)
    assert client.app.state.warn_version_drift(t0 + timedelta(hours=9999)) == 0
    assert _drift_events(logs) == []


def test_sweep_once_runs_the_drift_check(client_logs, monkeypatch):
    """Wired into the sweep, not just exposed as a seam. Threshold 0 so the
    second sweep warns without time-warping sweep_once's internal clock."""
    monkeypatch.setenv("JOBD_VERSION_DRIFT_WARN_HOURS", "0")
    client, logs = client_logs
    _add_worker(client, "stale-box", version="0.0.1")
    client.app.state.sweep_once()  # stamps the episode
    client.app.state.sweep_once()  # past a 0h threshold → warns
    assert len(_drift_events(logs)) == 1
