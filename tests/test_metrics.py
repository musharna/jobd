"""Tests for the Prometheus ``/metrics`` endpoint.

The endpoint is mounted as an ASGI sub-app so it bypasses the global bearer
token, and its path is exempted from the tailnet-IP ACL so an in-cluster
Prometheus (whose source IP is the docker bridge, not a tailnet address) can
scrape the broker's tailnet-bound port. It exposes only aggregate counts.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from jobd.app import build_app
from jobd.db import Job, Worker


@pytest.fixture
def app(tmp_path, sample_projects_yaml, sample_profiles_yaml, sample_classifier_yaml):
    return build_app(
        db_url=f"sqlite:///{tmp_path}/jobd.db",
        projects_path=sample_projects_yaml,
        profiles_path=sample_profiles_yaml,
        classifier_path=sample_classifier_yaml,
        logs_path=tmp_path / "logs",
    )


def _seed(app, *, jobs: dict[str, int], workers: dict[str, int]) -> None:
    now = datetime.now(UTC)
    with app.state.SessionLocal() as s:
        for state, n in jobs.items():
            for _ in range(n):
                s.add(
                    Job(
                        project="p",
                        priority=50,
                        state=state,
                        cmd_json="[]",
                        cwd="/tmp",
                        submitted_at=now,
                    )
                )
        for state, n in workers.items():
            for i in range(n):
                s.add(Worker(host=f"{state}-{i}", state=state, last_heartbeat=now))
        s.commit()


def test_metrics_reports_job_and_worker_counts(app):
    _seed(
        app,
        jobs={"queued": 3, "running": 1, "failed": 2},
        workers={"online": 2, "offline": 1},
    )
    body = TestClient(app).get("/metrics").text
    assert 'jobd_jobs{state="queued"} 3.0' in body
    assert 'jobd_jobs{state="running"} 1.0' in body
    assert 'jobd_jobs{state="failed"} 2.0' in body
    # Known-but-empty states are still emitted as 0 so dashboard panels never gap.
    assert 'jobd_jobs{state="completed"} 0.0' in body
    assert 'jobd_workers{state="online"} 2.0' in body
    assert 'jobd_workers{state="offline"} 1.0' in body
    assert "jobd_build_info{version=" in body


def test_collector_caches_db_query_within_ttl():
    """/metrics is unauthenticated + ACL-exempt, so an unauth scrape flood must
    not drive one GROUP BY per request. With a non-zero TTL the collector queries
    the DB once and serves the cache until the TTL elapses (audit LOW)."""
    from jobd.metrics import _JobdCollector

    calls = {"n": 0}

    class _Collector(_JobdCollector):
        def _query_counts(self):
            calls["n"] += 1
            return {"queued": 1}, {"online": 1}, [("gt76", "0.5.16")]

    c = _Collector(session_local=None, cache_ttl_s=60.0)
    list(c.collect())
    list(c.collect())
    families = list(c.collect())
    assert calls["n"] == 1  # three scrapes, one DB query

    # The cached counts are still emitted correctly.
    samples = {(f.name, s.labels.get("state")): s.value for f in families for s in f.samples}
    assert samples[("jobd_jobs", "queued")] == 1.0
    assert samples[("jobd_workers", "online")] == 1.0


def test_collector_requeries_after_ttl_expiry(monkeypatch):
    """Companion gap to the two tests around it (audit 2026-07-05): hit-within-
    TTL and TTL=0 were covered, but nothing pinned that the cache actually
    EXPIRES — a cache that never refreshed would freeze the dashboard at the
    first scrape's counts forever."""
    from types import SimpleNamespace

    from jobd import metrics as metrics_mod
    from jobd.metrics import _JobdCollector

    calls = {"n": 0}

    class _Collector(_JobdCollector):
        def _query_counts(self):
            calls["n"] += 1
            return {"queued": calls["n"]}, {}, []

    clock = {"t": 1000.0}
    monkeypatch.setattr(metrics_mod, "time", SimpleNamespace(monotonic=lambda: clock["t"]))
    c = _Collector(session_local=None, cache_ttl_s=5.0)
    list(c.collect())
    clock["t"] += 4.9
    list(c.collect())
    assert calls["n"] == 1  # still within TTL — served from cache

    clock["t"] += 0.2  # 5.1s since the query — TTL elapsed
    families = list(c.collect())
    assert calls["n"] == 2  # re-queried
    samples = {(f.name, s.labels.get("state")): s.value for f in families for s in f.samples}
    assert samples[("jobd_jobs", "queued")] == 2.0  # fresh value served


def test_collector_ttl_zero_disables_cache():
    """TTL=0 restores the pre-existing behavior: every scrape queries the DB."""
    from jobd.metrics import _JobdCollector

    calls = {"n": 0}

    class _Collector(_JobdCollector):
        def _query_counts(self):
            calls["n"] += 1
            return {}, {}, []

    c = _Collector(session_local=None, cache_ttl_s=0.0)
    list(c.collect())
    list(c.collect())
    assert calls["n"] == 2


def test_metrics_bypasses_bearer_token(app, monkeypatch):
    """Mount bypasses the global token: /metrics is 200 without a token while a
    normal route 401s."""
    monkeypatch.delenv("JOBD_ALLOW_NO_AUTH", raising=False)
    monkeypatch.setenv("JOBD_API_TOKEN", "s3cret")
    client = TestClient(app)
    assert client.get("/metrics").status_code == 200
    assert client.get("/health").status_code == 401


def test_metrics_exempt_from_tailnet_acl(app, monkeypatch):
    """The tailnet ACL 403s non-tailnet sources on normal routes but exempts
    /metrics so an in-cluster Prometheus can scrape it."""
    monkeypatch.delenv("JOBD_DISABLE_TAILNET_ACL", raising=False)
    # Simulate Prometheus reaching the broker from the docker bridge (non-tailnet).
    client = TestClient(app, client=("172.20.0.9", 5000))
    assert client.get("/metrics").status_code == 200
    assert client.get("/health").status_code == 403
