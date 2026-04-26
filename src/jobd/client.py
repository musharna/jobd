"""HTTP client wrapper for the jobd broker. Shared by job_cli and jobd.mcp."""

from __future__ import annotations

import os

import httpx


class BrokerUnreachable(Exception):
    """Network failure — DNS, connect refused, TLS, connect timeout."""


class BrokerServerError(Exception):
    """Broker returned 5xx."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class BrokerRefusal(Exception):
    """Broker returned 4xx with a `detail` body."""

    def __init__(self, message: str, *, status_code: int, detail: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class JobdClient:
    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: tuple[float, float] = (5.0, 30.0),
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("JOBD_URL") or "http://100.113.204.41:8765"
        ).rstrip("/")
        self._client = httpx.Client(
            timeout=httpx.Timeout(
                connect=timeout[0],
                read=timeout[1],
                write=timeout[1],
                pool=timeout[1],
            )
        )

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            r = self._client.request(method, f"{self.base_url}{path}", **kwargs)
        except (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.NetworkError,
        ) as e:
            raise BrokerUnreachable(f"{type(e).__name__}: {e} (JOBD_URL={self.base_url})") from e
        if 500 <= r.status_code < 600:
            raise BrokerServerError(
                f"broker {r.status_code}: {r.text[:500]}", status_code=r.status_code
            )
        if 400 <= r.status_code < 500:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            raise BrokerRefusal(
                f"broker {r.status_code}", status_code=r.status_code, detail=detail or ""
            )
        return r

    def submit(self, payload: dict) -> dict:
        return self._request("POST", "/submit", json=payload).json()

    def status(self, job_id: int) -> dict:
        return self._request("GET", f"/jobs/{job_id}").json()

    def cancel(self, job_id: int, *, reason: str | None = None) -> dict:
        body = {"reason": reason} if reason else None
        return self._request("POST", f"/jobs/{job_id}/cancel", json=body).json()

    def logs(self, job_id: int, *, tail_bytes: int = 8192) -> dict:
        return self._request("GET", f"/jobs/{job_id}/output", params={"tail": tail_bytes}).json()

    def list_jobs(self, *, state: str | None = None, project: str | None = None) -> dict:
        params: dict[str, str] = {}
        if state:
            params["state_filter"] = state
        if project:
            params["project"] = project
        return self._request("GET", "/jobs", params=params).json()

    def workers(self) -> dict:
        return self._request("GET", "/workers").json()

    def job_get(self, job_id: int) -> dict:
        return self._request("GET", f"/jobs/{job_id}").json()

    def __enter__(self) -> "JobdClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._client.close()
