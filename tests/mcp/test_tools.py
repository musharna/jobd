from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import respx
from jobd.client import JobdClient
from jobd.mcp.tools import jobd_submit


@respx.mock
def test_submit_async_returns_job_id_and_state():
    respx.post("http://broker.test/submit").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": 7,
                "state": "queued",
                "project": "p",
                "host_pin": "any",
                "queued_at": "2026-04-26T00:00:00Z",
            },
        )
    )
    client = JobdClient(base_url="http://broker.test")
    out = jobd_submit(client, {"command": "x", "project": "p", "cwd": "/x"})
    assert out["job_id"] == 7
    assert out["state"] == "queued"
    assert "warning" not in out  # no warning by default


@respx.mock
def test_submit_async_surfaces_broker_warning():
    respx.post("http://broker.test/submit").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": 8,
                "state": "queued",
                "project": "p",
                "host_pin": "laptop",
                "queued_at": "t",
                "warning": "no laptop worker has heartbeat in 24h",
            },
        )
    )
    client = JobdClient(base_url="http://broker.test")
    out = jobd_submit(client, {"command": "x", "project": "p", "cwd": "/x", "host": "laptop"})
    assert out["warning"] == "no laptop worker has heartbeat in 24h"


@respx.mock
def test_submit_extra_keys_merge_into_payload():
    route = respx.post("http://broker.test/submit").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": 9,
                "state": "queued",
                "project": "p",
                "host_pin": "any",
                "queued_at": "t",
            },
        )
    )
    client = JobdClient(base_url="http://broker.test")
    jobd_submit(
        client,
        {
            "command": "x",
            "project": "p",
            "cwd": "/x",
            "extra": {"idempotent": True, "depends_on": [1, 2], "max_wall": "1h"},
        },
    )
    body = route.calls.last.request.content.decode()
    assert '"idempotent":true' in body or '"idempotent": true' in body
    assert "depends_on" in body
    assert "max_wall" in body


@respx.mock
def test_jobd_status_async_returns_full_info():
    respx.get("http://broker.test/jobs/7").mock(
        return_value=httpx.Response(
            200, json={"job_id": 7, "state": "running", "exit_code": None, "host": "desktop"}
        )
    )
    from jobd.mcp.tools import jobd_status

    client = JobdClient(base_url="http://broker.test")
    out = jobd_status(client, {"job_id": 7})
    assert out["state"] == "running"
    assert out["host"] == "desktop"


@respx.mock
def test_jobd_status_wait_returns_timeout_when_running():
    respx.get("http://broker.test/jobs/7").mock(
        return_value=httpx.Response(200, json={"job_id": 7, "state": "running", "exit_code": None})
    )
    from jobd.mcp.tools import jobd_status

    client = JobdClient(base_url="http://broker.test")
    times = iter([0.0, 11.0])
    with (
        patch("jobd.mcp.tools.time.monotonic", side_effect=lambda: next(times)),
        patch("jobd.mcp.tools.time.sleep"),
    ):
        out = jobd_status(client, {"job_id": 7, "wait": True, "wait_timeout_s": 10})
    assert out["timed_out"] is True


@respx.mock
def test_jobd_logs_passes_tail_bytes_through():
    route = respx.get("http://broker.test/jobs/7/output").mock(
        return_value=httpx.Response(
            200,
            json={
                "tail": "abc",
                "size_bytes": 3,
                "returned_bytes": 3,
                "truncated": False,
                "has_log": True,
            },
        )
    )
    from jobd.mcp.tools import jobd_logs

    client = JobdClient(base_url="http://broker.test")
    out = jobd_logs(client, {"job_id": 7, "tail_bytes": 1000})
    assert out["tail"] == "abc"
    assert route.calls.last.request.url.params["tail"] == "1000"


@respx.mock
def test_jobd_cancel_returns_prior_and_new_state():
    respx.get("http://broker.test/jobs/7").mock(
        side_effect=[
            httpx.Response(200, json={"job_id": 7, "state": "running"}),  # prior
            httpx.Response(
                200, json={"job_id": 7, "state": "running", "signal": "cancel"}
            ),  # post-cancel
        ]
    )
    respx.post("http://broker.test/jobs/7/cancel").mock(
        return_value=httpx.Response(200, json={"job_id": 7, "state": "running", "signal": "cancel"})
    )
    from jobd.mcp.tools import jobd_cancel

    client = JobdClient(base_url="http://broker.test")
    out = jobd_cancel(client, {"job_id": 7, "reason": "test"})
    assert out["prior_state"] == "running"
    assert out["new_state"] == "running"
    assert out["signal_sent"] == "cancel"


@respx.mock
def test_jobd_list_summarizes_jobs():
    respx.get("http://broker.test/jobs").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "job_id": 1,
                        "project": "p",
                        "state": "queued",
                        "host": None,
                        "exit_code": None,
                        "queued_at": "t",
                        "started_at": None,
                        "extra_field_dropped": "x",
                    },
                    {
                        "job_id": 2,
                        "project": "p",
                        "state": "running",
                        "host": "desktop",
                        "exit_code": None,
                        "queued_at": "t",
                        "started_at": "t",
                    },
                ],
                "counts": {"queued": 1, "running": 1, "recent_failed_24h": 0},
            },
        )
    )
    from jobd.mcp.tools import jobd_list

    client = JobdClient(base_url="http://broker.test")
    out = jobd_list(client, {"state": ["queued", "running"]})
    assert out["counts"]["queued"] == 1
    assert len(out["jobs"]) == 2
    assert "extra_field_dropped" not in out["jobs"][0]
    assert set(out["jobs"][0].keys()) == {
        "job_id",
        "project",
        "state",
        "host",
        "exit_code",
        "queued_at",
        "started_at",
    }


@respx.mock
def test_jobd_workers_healthy_when_recent_heartbeat():
    recent = datetime.now(timezone.utc).isoformat()
    respx.get("http://broker.test/workers").mock(
        return_value=httpx.Response(
            200, json={"workers": [{"host": "desktop", "last_heartbeat": recent}]}
        )
    )
    from jobd.mcp.tools import jobd_workers

    client = JobdClient(base_url="http://broker.test")
    out = jobd_workers(client, {})
    assert out["fleet_health"] == "healthy"
    assert out["warnings"] == []


@respx.mock
def test_jobd_workers_degraded_when_stale():
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    respx.get("http://broker.test/workers").mock(
        return_value=httpx.Response(
            200, json={"workers": [{"host": "desktop", "last_heartbeat": old}]}
        )
    )
    from jobd.mcp.tools import jobd_workers

    client = JobdClient(base_url="http://broker.test")
    out = jobd_workers(client, {})
    assert out["fleet_health"] == "degraded"
    assert any("stale" in w for w in out["warnings"])


@respx.mock
def test_jobd_workers_empty_fleet():
    respx.get("http://broker.test/workers").mock(
        return_value=httpx.Response(200, json={"workers": []})
    )
    from jobd.mcp.tools import jobd_workers

    client = JobdClient(base_url="http://broker.test")
    out = jobd_workers(client, {})
    assert out["fleet_health"] == "empty"
