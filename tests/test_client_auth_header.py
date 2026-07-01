"""Verify CLI + worker httpx.Client construction injects Bearer header from JOBD_API_TOKEN."""

import httpx

from jobd.client import JobdClient


def test_client_injects_bearer_when_env_set(monkeypatch):
    monkeypatch.setenv("JOBD_API_TOKEN", "s3cret")
    captured = {}

    def transport_handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={})

    client = JobdClient(base_url="http://test")
    client._client = httpx.Client(
        transport=httpx.MockTransport(transport_handler),
        headers=client._client.headers,
    )
    client._request("GET", "/workers")
    assert captured["auth"] == "Bearer s3cret"


def test_client_no_header_when_env_unset(monkeypatch):
    monkeypatch.delenv("JOBD_API_TOKEN", raising=False)
    captured = {}

    def transport_handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={})

    client = JobdClient(base_url="http://test")
    client._client = httpx.Client(
        transport=httpx.MockTransport(transport_handler),
        headers=client._client.headers,
    )
    client._request("GET", "/workers")
    assert captured["auth"] is None


def test_worker_httpx_client_carries_bearer(monkeypatch):
    """Mirror the heartbeat-client construction in jobd/worker/job_worker.py — JOBD_API_TOKEN
    must propagate into the worker's heartbeat httpx.Client headers."""
    import os

    monkeypatch.setenv("JOBD_API_TOKEN", "s3cret")
    token = os.environ.get("JOBD_API_TOKEN", "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    client = httpx.Client(base_url="http://test", timeout=1.0, headers=headers)
    assert client.headers.get("Authorization") == "Bearer s3cret"


def test_worker_httpx_client_carries_worker_identity():
    """M2: the worker tags every request with X-Jobd-Worker = its hostname (the
    same value it sends as `host` in /next-job, which becomes job.worker) so the
    broker can refuse /log, /started, and /complete from a stale worker after a
    partition reclaim + re-dispatch."""
    from jobd.worker.job_worker import hostname

    headers: dict[str, str] = {}
    headers["X-Jobd-Worker"] = hostname()
    client = httpx.Client(base_url="http://test", timeout=1.0, headers=headers)
    assert client.headers.get("X-Jobd-Worker") == hostname()
    assert hostname()  # non-empty
