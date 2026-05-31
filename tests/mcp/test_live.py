"""Live-broker integration test for the MCP translation layer.

Skipped by default. Opt in with `pytest -m live` or `RUN_LIVE_JOBD=1 pytest`.
Hits the real jobd broker (`JOBD_URL` env, default `http://127.0.0.1:8765`)
and proves the translation layer's outbound + inbound shapes survive contact
with the real schema. This is the sentinel that prevents synthetic-fixture
green from masking schema drift again.

The test does NOT require the broker to actually run the job to terminal —
the desktop worker is shared and may be bottlenecked. It asserts schema
shape on submit/status/list/workers/job_get plus a successful cancel.
"""

from __future__ import annotations

import os
import time

import pytest

from jobd.client import JobdClient
from jobd.mcp.tools import (
    jobd_cancel,
    jobd_job_get,
    jobd_list,
    jobd_status,
    jobd_submit,
    jobd_workers,
)

LIVE = os.environ.get("RUN_LIVE_JOBD") == "1"
JOBD_URL = os.environ.get("JOBD_URL", "http://127.0.0.1:8765")


def _broker_reachable() -> bool:
    try:
        with JobdClient(base_url=JOBD_URL, timeout=(3.0, 3.0)) as c:
            c.get("/workers")
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not LIVE, reason="set RUN_LIVE_JOBD=1 to opt in"),
    pytest.mark.skipif(
        not _broker_reachable() if LIVE else False, reason=f"broker {JOBD_URL} unreachable"
    ),
]


def test_live_workers_returns_envelope_with_fleet_health():
    client = JobdClient(base_url=JOBD_URL)
    out = jobd_workers(client, {})
    assert "workers" in out
    assert "fleet_health" in out
    assert out["fleet_health"] in ("healthy", "degraded", "empty")
    assert isinstance(out["workers"], list)
    if out["workers"]:
        w = out["workers"][0]
        assert "host" in w
        assert "last_heartbeat" in w


def test_live_submit_status_list_jobget_cancel_full_round_trip():
    """End-to-end: outbound translation + inbound translation + cancel.

    Submits a `true` job (will sit in the queue if the worker is busy —
    that's fine, we don't need it to run). Asserts every translated
    field is present and shaped correctly, then cancels to clean up.
    """
    client = JobdClient(base_url=JOBD_URL)

    # Submit — exercises xlate_submit_payload (cmd wrap, host_pin) +
    # xlate_job_info on the response (id→job_id, submitted_at→queued_at).
    sub = jobd_submit(
        client,
        {
            "command": "true",
            "project": "jobd-mcp-live-test",
            "cwd": "/tmp",
            "extra": {"idempotent": True},
        },
    )
    assert isinstance(sub["job_id"], int), f"submit returned no job_id: {sub}"
    job_id = sub["job_id"]
    assert sub["state"] in {"queued", "assigned", "running", "completed"}
    assert sub["project"] == "jobd-mcp-live-test"
    assert sub["host_pin"] == "any"
    assert "queued_at" in sub  # renamed from broker's submitted_at

    try:
        # Status — exercises xlate_job_info on /jobs/<id>.
        st = jobd_status(client, {"job_id": job_id})
        assert st["job_id"] == job_id  # renamed from id
        assert "host" in st  # renamed from worker (None when queued)
        assert "duration_s" in st  # synthesized — None when not finished
        assert "signal" in st  # synthesized — None unless cancel ran

        # List — exercises wrap_jobs (bare list → {jobs, counts}).
        lst = jobd_list(client, {"project": "jobd-mcp-live-test"})
        assert "counts" in lst
        assert "jobs" in lst
        ids = {j["job_id"] for j in lst["jobs"]}
        assert job_id in ids, f"submitted job_id {job_id} not in list: {ids}"
        # summary shape — only the keys jobd_list promises.
        assert set(lst["jobs"][0].keys()) == {
            "job_id",
            "project",
            "state",
            "host",
            "exit_code",
            "queued_at",
            "started_at",
        }

        # Job get — exercises xlate_job_info on /jobs/<id> (broker's full info).
        jg = jobd_job_get(client, {"job_id": job_id})
        assert jg["job_id"] == job_id
        assert jg["cmd"] == ["bash", "-c", "true"]
        assert jg["cwd"] == "/tmp"
        # `requires` may be a dict or absent depending on broker version;
        # if present, idempotent=True must round-trip.
        if jg.get("requires"):
            assert jg["requires"].get("idempotent") is True

        # Brief poll — let the worker pick it up if it's idle. Don't fail
        # on timeout; the queue may be busy with other agents' GPU work.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            cur = jobd_status(client, {"job_id": job_id})
            if cur["state"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.5)
    finally:
        # Cancel — exercises jobd_cancel's prior/new state machinery.
        # Always run, even if the job already finished (idempotent on broker).
        cancel_out = jobd_cancel(client, {"job_id": job_id, "reason": "live test cleanup"})
        assert cancel_out["job_id"] == job_id
        assert "prior_state" in cancel_out
        assert "new_state" in cancel_out
        assert "signal_sent" in cancel_out  # synthesized field
