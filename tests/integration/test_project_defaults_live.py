"""Live-broker integration test for project defaults resolution.

Gated on ``RUN_LIVE_JOBD=1`` env var (and ``JOBD_URL`` pointing at a live
broker). Submits a job whose project entry has known ``defaults`` and reads
the resulting Job back to confirm the broker applied them.

Cannot run in unit-test mode because a TestClient process is exactly what
``test_resolution_order.py`` already covers — this test exists to catch the
class of bug where the wire schema, the docker stack's projects.yaml, or the
Pydantic models disagree across processes.
"""

from __future__ import annotations

import os

import pytest

from jobd.client import JobdClient

LIVE = os.environ.get("RUN_LIVE_JOBD") == "1"
BASE = os.environ.get("JOBD_URL", "http://127.0.0.1:8765")


pytestmark = pytest.mark.skipif(
    not LIVE,
    reason="set RUN_LIVE_JOBD=1 (and JOBD_URL) to run live integration tests",
)


def _broker_reachable() -> bool:
    try:
        with JobdClient(base_url=BASE, timeout=(2.0, 2.0)) as c:
            c.get("/health")
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    not _broker_reachable(),
    reason="broker at JOBD_URL not reachable",
)
def test_project_defaults_applied_to_db_row():
    """Submit a project that has defaults, expect the row to reflect them.

    NOTE: this assumes the live broker's projects.yaml has at least one
    project with a non-trivial defaults block. The test reads via
    /resolve first to discover the configured values, then submits and
    asserts the JobInfo response matches the resolved-source values that
    came from project_default.
    """
    with JobdClient(base_url=BASE) as c:
        # Pick the first project entry whose /resolve report tags any field as
        # source=project_default. If none exists on the live broker, skip.
        list_r = c.get("/projects")
        candidates = [
            name
            for name, entry in list_r.json().items()
            if name != "_default" and isinstance(entry, dict) and entry.get("defaults")
        ]
        if not candidates:
            pytest.skip("no project on live broker has a defaults block")
        project = candidates[0]

        resolved = c.post(
            "/resolve", json={"cmd": ["true"], "cwd": "/tmp", "project": project}
        ).json()
        job = c.post("/submit", json={"cmd": ["true"], "cwd": "/tmp", "project": project}).json()
        try:
            # Resolved values should match the persisted Job row.
            if resolved["effective_max_wall_s"]["source"] == "project_default":
                assert job["max_wall_s"] == resolved["effective_max_wall_s"]["value"]
            if resolved["effective_idle_timeout_s"]["source"] == "project_default":
                assert job["idle_timeout_s"] == resolved["effective_idle_timeout_s"]["value"]
            if resolved["effective_host_pin"]["source"] == "project_default":
                assert job["host_pin"] == resolved["effective_host_pin"]["value"]
            if resolved["effective_preemptible"]["source"] == "project_default":
                assert job["preemptible"] == resolved["effective_preemptible"]["value"]
        finally:
            # Clean up: cancel before any worker picks it up.
            try:
                c.post(f"/jobs/{job['id']}/cancel")
            except Exception:
                pass
