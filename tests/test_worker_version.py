"""Worker version reporting (improvement audit 2026-07-12).

Workers are upgraded host-by-host over SSH, so the fleet routinely runs mixed
versions — and before this the broker had no way to know. A worker running code
from three releases ago was indistinguishable from a current one, which made the
question "is any worker stale?" unanswerable from the broker, precisely the
question the worker's single-file drift history keeps raising.
"""

from __future__ import annotations

import re

from jobd import __version__


# Deliberately synthetic version strings: these are heartbeat *payload* values,
# not the package version. A real release number here would be picked up by the
# "grep the repo for the old version" sweep every release cut runs, and read as
# something that needs bumping.
def _heartbeat(client, host: str, **overrides):
    body = {
        "host": host,
        "free_vram_gb": 8.0,
        "unregistered_vram_gb": 0.0,
        "free_ram_gb": 16.0,
        "idle_cpus": 4,
    }
    body.update(overrides)
    r = client.post("/heartbeat", json=body)
    assert r.status_code == 200, r.text
    return r


def _worker(client, host: str) -> dict:
    rows = client.get("/workers").json()
    return next(w for w in rows if w["host"] == host)


def test_heartbeat_version_surfaces_on_workers(client):
    _heartbeat(client, "gt76", version="1.2.3")
    assert _worker(client, "gt76")["version"] == "1.2.3"


def test_worker_without_version_reads_as_none_not_a_lie(client):
    """A worker too old to report a version must read as `None` — not as the
    broker's version, and not as a missing key. The absence IS the signal."""
    _heartbeat(client, "ancient")
    assert _worker(client, "ancient")["version"] is None


def test_version_is_not_pinned_to_last_known_value(client):
    """A downgrade (or a worker reverting to a build that doesn't report) must
    not leave the last-known version stuck in the registry — that would make the
    fleet look newer than it is, which is worse than not knowing."""
    _heartbeat(client, "gt76", version="1.2.3")
    assert _worker(client, "gt76")["version"] == "1.2.3"
    _heartbeat(client, "gt76")  # older build, reports nothing
    assert _worker(client, "gt76")["version"] is None


def test_worker_reports_its_own_installed_version():
    """The worker's heartbeat snapshot must carry the real jobd version, not a
    hardcoded string that can drift from the package it ships in."""
    from jobd.worker.job_worker import pick_resource_snapshot_mock

    assert pick_resource_snapshot_mock()["version"] == __version__


def test_metrics_expose_worker_version_series(client):
    """Drift has to be ALERTABLE, not merely visible: pair
    jobd_worker_version_info with jobd_build_info and Prometheus can answer both
    "is any worker adrift from the broker?" and "is the fleet uniform?"."""
    _heartbeat(client, "gt76", version="1.2.3")
    _heartbeat(client, "msi-4080", version="1.2.2")
    _heartbeat(client, "ancient")

    body = client.get("/metrics").text
    assert 'jobd_worker_version_info{host="gt76",version="1.2.3"} 1.0' in body
    assert 'jobd_worker_version_info{host="msi-4080",version="1.2.2"} 1.0' in body
    # a non-reporting worker is labelled, not omitted — a missing series would
    # silently shrink the drift count instead of flagging it
    assert 'jobd_worker_version_info{host="ancient",version="unknown"} 1.0' in body
    assert re.search(r'jobd_build_info\{version="[^"]+"\} 1\.0', body)
