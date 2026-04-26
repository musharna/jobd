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
