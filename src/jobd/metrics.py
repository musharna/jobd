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

from collections.abc import Iterator

from prometheus_client import CollectorRegistry, make_asgi_app
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from jobd import __version__
from jobd.db import Job, Worker
from jobd.models import JobState

_WORKER_STATES: tuple[str, ...] = ("online", "offline")


def _ordered_states(known: tuple[str, ...] | list[str], counts: dict[str, int]) -> list[str]:
    """Known states first (emitted as 0 when absent so dashboard panels never
    gap), then any unknown states present in the DB (forward-compat)."""
    known_list = list(known)
    seen = set(known_list)
    return known_list + [s for s in counts if s not in seen]


class _JobdCollector:
    """prometheus_client custom collector: queries the broker DB on each scrape."""

    def __init__(self, session_local: sessionmaker) -> None:
        self._session_local = session_local

    def collect(self) -> Iterator[GaugeMetricFamily]:
        with self._session_local() as session:
            job_counts: dict[str, int] = dict(
                session.execute(select(Job.state, func.count()).group_by(Job.state)).all()
            )
            worker_counts: dict[str, int] = dict(
                session.execute(select(Worker.state, func.count()).group_by(Worker.state)).all()
            )

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
