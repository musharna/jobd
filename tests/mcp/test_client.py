import httpx
import pytest
import respx

from jobd.client import BrokerRefusal, BrokerServerError, BrokerUnreachable, JobdClient


def test_broker_unreachable_is_exception():
    with pytest.raises(BrokerUnreachable):
        raise BrokerUnreachable("connect refused")


def test_broker_server_error_carries_status():
    e = BrokerServerError("oops", status_code=503)
    assert e.status_code == 503


def test_broker_refusal_carries_detail_and_status():
    e = BrokerRefusal(
        "cwd outside mount_roots", status_code=400, detail="cwd '/mnt/c/...' is under /mnt/c/..."
    )
    assert e.status_code == 400
    assert "cwd" in e.detail


@respx.mock
def test_submit_posts_to_jobs_and_returns_job_info():
    respx.post("http://broker.test/submit").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": 42,
                "state": "queued",
                "project": "project-b",
                "host_pin": "any",
                "queued_at": "2026-04-26T12:00:00Z",
            },
        )
    )
    c = JobdClient(base_url="http://broker.test")
    out = c.submit({"command": "sleep 1", "project": "project-b", "cwd": "/home/x"})
    assert out["job_id"] == 42
    assert out["state"] == "queued"


@respx.mock
def test_submit_400_raises_broker_refusal():
    respx.post("http://broker.test/submit").mock(
        return_value=httpx.Response(
            400, json={"detail": "cwd '/mnt/c/...' is under /mnt/c/... pass --host laptop"}
        )
    )
    c = JobdClient(base_url="http://broker.test")
    with pytest.raises(BrokerRefusal) as excinfo:
        c.submit({"command": "x", "project": "p", "cwd": "/mnt/c/Users/x"})
    assert excinfo.value.status_code == 400
    assert "/mnt/c/" in excinfo.value.detail


@respx.mock
def test_submit_500_raises_broker_server_error():
    respx.post("http://broker.test/submit").mock(return_value=httpx.Response(503))
    c = JobdClient(base_url="http://broker.test")
    with pytest.raises(BrokerServerError) as excinfo:
        c.submit({"command": "x", "project": "p", "cwd": "/x"})
    assert excinfo.value.status_code == 503


@respx.mock
def test_submit_connection_error_raises_broker_unreachable():
    respx.post("http://broker.test/submit").mock(side_effect=httpx.ConnectError("refused"))
    c = JobdClient(base_url="http://broker.test")
    with pytest.raises(BrokerUnreachable):
        c.submit({"command": "x", "project": "p", "cwd": "/x"})


@respx.mock
def test_status_returns_job_info():
    respx.get("http://broker.test/jobs/42").mock(
        return_value=httpx.Response(200, json={"job_id": 42, "state": "running", "exit_code": None})
    )
    c = JobdClient(base_url="http://broker.test")
    info = c.status(42)
    assert info["state"] == "running"


@respx.mock
def test_status_404_raises_refusal():
    respx.get("http://broker.test/jobs/9999").mock(
        return_value=httpx.Response(404, json={"detail": "no job 9999"})
    )
    c = JobdClient(base_url="http://broker.test")
    with pytest.raises(BrokerRefusal) as excinfo:
        c.status(9999)
    assert excinfo.value.status_code == 404


@respx.mock
def test_cancel_posts_to_cancel_endpoint():
    respx.post("http://broker.test/jobs/42/cancel").mock(
        return_value=httpx.Response(200, json={"job_id": 42, "state": "cancelled", "signal": None})
    )
    c = JobdClient(base_url="http://broker.test")
    out = c.cancel(42, reason="user")
    assert out["state"] == "cancelled"


@respx.mock
def test_logs_passes_tail_query_param():
    route = respx.get("http://broker.test/jobs/42/output").mock(
        return_value=httpx.Response(
            200,
            json={
                "tail": "hello",
                "size_bytes": 5,
                "returned_bytes": 5,
                "truncated": False,
                "has_log": True,
            },
        )
    )
    c = JobdClient(base_url="http://broker.test")
    out = c.logs(42, tail_bytes=4096)
    assert out["tail"] == "hello"
    assert route.calls.last.request.url.params["tail"] == "4096"


@respx.mock
def test_list_jobs_passes_filters():
    route = respx.get("http://broker.test/jobs").mock(
        return_value=httpx.Response(
            200, json={"jobs": [], "counts": {"queued": 0, "running": 0, "recent_failed_24h": 0}}
        )
    )
    c = JobdClient(base_url="http://broker.test")
    out = c.list_jobs(state="queued", project="project-b")
    assert out["counts"]["queued"] == 0
    params = route.calls.last.request.url.params
    assert params["state_filter"] == "queued"
    assert params["project"] == "project-b"


@respx.mock
def test_workers_returns_worker_list():
    respx.get("http://broker.test/workers").mock(
        return_value=httpx.Response(
            200,
            json={
                "workers": [
                    {
                        "host": "desktop",
                        "host_aliases": ["any", "any-gpu"],
                        "last_heartbeat": "2026-04-26T12:00:00Z",
                    }
                ]
            },
        )
    )
    c = JobdClient(base_url="http://broker.test")
    out = c.workers()
    assert len(out["workers"]) == 1


@respx.mock
def test_job_get_returns_full_job_info():
    respx.get("http://broker.test/jobs/42").mock(
        return_value=httpx.Response(
            200,
            json={"job_id": 42, "command": "sleep 1", "depends_on": [], "fast_path": False},
        )
    )
    c = JobdClient(base_url="http://broker.test")
    info = c.job_get(42)
    assert info["job_id"] == 42
    assert "fast_path" in info


def test_client_context_manager_closes_underlying_httpx():
    with JobdClient(base_url="http://broker.test") as c:
        assert c._client is not None
    assert c._client.is_closed
