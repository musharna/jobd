"""Prometheus ``/metrics`` endpoint for the jobd broker.

Exposes aggregate job + worker counts as gauges, computed from the broker DB on
each scrape. Wired in ``app.build_app`` as a mounted ASGI sub-app, which bypasses
the global bearer-token dependency (see ``auth.require_token``); the ``/metrics``
path is additionally exempted from the tailnet-IP ACL (see
``auth.TailnetACLMiddleware``) so an in-cluster Prometheus — whose source IP is
the docker bridge, not a tailnet address — can scrape the broker's tailnet-bound
port. Only non-sensitive aggregate counts are exposed.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator

from prometheus_client import CollectorRegistry, make_asgi_app
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from jobd import __version__
from jobd.db import Job, Worker
from jobd.models import JobState

_WORKER_STATES: tuple[str, ...] = ("online", "offline")

# /metrics is unauthenticated AND tailnet-ACL-exempt by design (an in-cluster
# Prometheus scrapes from the docker-bridge IP, not a tailnet address — see the
# module docstring). That makes the per-scrape DB query an amplification vector:
# an unauthenticated caller can drive one GROUP BY per request in a tight loop.
# Cache the aggregate counts for a short TTL so the DB is hit at most once per
# window regardless of scrape rate. Tunable via JOBD_METRICS_CACHE_TTL_S; set to
# 0 to disable (always query — real-time counts, pre-existing behavior).
_DEFAULT_CACHE_TTL_S = 5.0


def _ordered_states(known: tuple[str, ...] | list[str], counts: dict[str, int]) -> list[str]:
    """Known states first (emitted as 0 when absent so dashboard panels never
    gap), then any unknown states present in the DB (forward-compat)."""
    known_list = list(known)
    seen = set(known_list)
    return known_list + [s for s in counts if s not in seen]


class _JobdCollector:
    """prometheus_client custom collector: aggregate job/worker counts from the
    broker DB, cached for a short TTL so an unauthenticated /metrics scrape flood
    can't drive one GROUP BY per request."""

    def __init__(self, session_local: sessionmaker, cache_ttl_s: float | None = None) -> None:
        self._session_local = session_local
        if cache_ttl_s is None:
            cache_ttl_s = float(os.environ.get("JOBD_METRICS_CACHE_TTL_S", _DEFAULT_CACHE_TTL_S))
        self._cache_ttl_s = cache_ttl_s
        self._lock = threading.Lock()
        self._cache: tuple[dict[str, int], dict[str, int]] | None = None
        self._cached_at = 0.0

    def _query_counts(self) -> tuple[dict[str, int], dict[str, int]]:
        with self._session_local() as session:
            job_counts: dict[str, int] = dict(
                session.execute(select(Job.state, func.count()).group_by(Job.state)).all()
            )
            worker_counts: dict[str, int] = dict(
                session.execute(select(Worker.state, func.count()).group_by(Worker.state)).all()
            )
        return job_counts, worker_counts

    def _counts(self) -> tuple[dict[str, int], dict[str, int]]:
        """Return (job_counts, worker_counts), served from a TTL cache. The query
        runs under the lock so a scrape burst that all misses the cache issues a
        single DB round-trip (dogpile-proof), not one per concurrent request. A
        TTL of 0 disables caching (always query — real-time counts)."""
        if self._cache_ttl_s <= 0:
            return self._query_counts()
        with self._lock:
            now = time.monotonic()
            if self._cache is None or (now - self._cached_at) >= self._cache_ttl_s:
                self._cache = self._query_counts()
                self._cached_at = now
            return self._cache

    def collect(self) -> Iterator[GaugeMetricFamily]:
        job_counts, worker_counts = self._counts()

        jobs = GaugeMetricFamily(
            "jobd_jobs",
            "Number of jobs in the broker database, by state.",
            labels=["state"],
        )
        for state in _ordered_states([s.value for s in JobState], job_counts):
            jobs.add_metric([state], float(job_counts.get(state, 0)))
        yield jobs

        workers = GaugeMetricFamily(
            "jobd_workers",
            "Number of registered workers, by state.",
            labels=["state"],
        )
        for state in _ordered_states(_WORKER_STATES, worker_counts):
            workers.add_metric([state], float(worker_counts.get(state, 0)))
        yield workers

        info = GaugeMetricFamily(
            "jobd_build_info",
            "jobd broker build info; the series value is always 1.",
            labels=["version"],
        )
        info.add_metric([__version__], 1.0)
        yield info


def build_metrics_app(session_local: sessionmaker):
    """Return an ASGI app serving Prometheus text for the given session factory.

    Uses a private registry (no default process/platform collectors) so
    ``/metrics`` contains only ``jobd_*`` series.
    """
    registry = CollectorRegistry()
    registry.register(_JobdCollector(session_local))
    return make_asgi_app(registry)
