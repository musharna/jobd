from unittest.mock import patch

import httpx
import respx

from jobd.client import JobdClient
from jobd.mcp.tools import jobd_submit


@respx.mock
def test_submit_wait_returns_terminal_with_logs():
    respx.post("http://broker.test/submit").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": 7,
                "state": "queued",
                "project": "p",
                "host_pin": "any",
                "queued_at": "t",
            },
        )
    )
    statuses = iter(
        [
            httpx.Response(
                200,
                json={
                    "job_id": 7,
                    "state": "running",
                    "exit_code": None,
                    "started_at": "t",
                    "finished_at": None,
                },
            ),
            httpx.Response(
                200,
                json={
                    "job_id": 7,
                    "state": "completed",
                    "exit_code": 0,
                    "started_at": "t",
                    "finished_at": "t2",
                },
            ),
        ]
    )
    respx.get("http://broker.test/jobs/7").mock(side_effect=lambda req: next(statuses))
    respx.get("http://broker.test/jobs/7/output").mock(
        return_value=httpx.Response(
            200,
            json={
                "tail": "done",
                "size_bytes": 4,
                "returned_bytes": 4,
                "truncated": False,
                "has_log": True,
            },
        )
    )

    client = JobdClient(base_url="http://broker.test")
    with patch("jobd.mcp.tools.time.sleep"):  # don't actually sleep in tests
        out = jobd_submit(
            client,
            {"command": "x", "project": "p", "cwd": "/x", "wait": True, "wait_timeout_s": 30},
        )
    assert out["state"] == "completed"
    assert out["exit_code"] == 0
    assert out["log_tail"] == "done"


@respx.mock
def test_submit_wait_returns_timeout_when_running():
    respx.post("http://broker.test/submit").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": 7,
                "state": "queued",
                "project": "p",
                "host_pin": "any",
                "queued_at": "t",
            },
        )
    )
    respx.get("http://broker.test/jobs/7").mock(
        return_value=httpx.Response(200, json={"job_id": 7, "state": "running", "exit_code": None})
    )

    client = JobdClient(base_url="http://broker.test")
    times = iter([0.0, 0.0, 1.0, 6.0, 11.0])  # last value > wait_timeout_s=10 → timeout
    with (
        patch("jobd.mcp.tools.time.monotonic", side_effect=lambda: next(times)),
        patch("jobd.mcp.tools.time.sleep"),
    ):
        out = jobd_submit(
            client,
            {"command": "x", "project": "p", "cwd": "/x", "wait": True, "wait_timeout_s": 10},
        )
    assert out["state"] == "running"
    assert out["timed_out"] is True
    assert "hint" in out


@respx.mock
def test_submit_wait_clamps_above_270():
    respx.post("http://broker.test/submit").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": 7,
                "state": "completed",
                "exit_code": 0,
                "project": "p",
                "host_pin": "any",
                "queued_at": "t",
            },
        )
    )
    # state already terminal at submit response — skip status poll entirely
    respx.get("http://broker.test/jobs/7").mock(
        return_value=httpx.Response(200, json={"job_id": 7, "state": "completed", "exit_code": 0})
    )
    respx.get("http://broker.test/jobs/7/output").mock(
        return_value=httpx.Response(
            200,
            json={
                "tail": "x",
                "size_bytes": 1,
                "returned_bytes": 1,
                "truncated": False,
                "has_log": True,
            },
        )
    )
    client = JobdClient(base_url="http://broker.test")
    with patch("jobd.mcp.tools.time.sleep"):
        out = jobd_submit(
            client,
            {"command": "x", "project": "p", "cwd": "/x", "wait": True, "wait_timeout_s": 9999},
        )
    assert out.get("clamped") is True
