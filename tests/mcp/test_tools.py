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
