"""HTTP client wrapper for the jobd broker. Shared by job_cli and jobd.mcp.

Also exposes workload-side preemption helpers (`install_preemption_handler`,
`time_remaining`) that user training scripts call to participate in the
broker's preempt + checkpoint protocol.
"""

from __future__ import annotations

import os
import signal as _signal
import time as _time
from collections.abc import Callable

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
        self.base_url = (base_url or os.environ.get("JOBD_URL") or "http://127.0.0.1:8765").rstrip(
            "/"
        )
        _token = os.environ.get("JOBD_API_TOKEN", "").strip()
        _headers = {"Authorization": f"Bearer {_token}"} if _token else {}
        self._client = httpx.Client(
            timeout=httpx.Timeout(
                connect=timeout[0],
                read=timeout[1],
                write=timeout[1],
                pool=timeout[1],
            ),
            headers=_headers,
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

    def preempt(self, job_id: int) -> dict:
        return self._request("POST", f"/jobs/{job_id}/preempt").json()

    def checkpoint_complete(self, job_id: int) -> dict:
        return self._request("POST", f"/jobs/{job_id}/checkpoint-complete").json()

    def logs(self, job_id: int, *, tail_bytes: int = 8192) -> dict:
        return self._request("GET", f"/jobs/{job_id}/output", params={"tail": tail_bytes}).json()

    def list_jobs(self, *, state: str | None = None, project: str | None = None) -> dict:
        params: dict[str, str] = {}
        if state:
            params["state_filter"] = state
        if project:
            params["project"] = project
        return self._request("GET", "/jobs", params=params).json()

    def events(
        self,
        *,
        since: str | None = None,
        project: str | None = None,
        event: str | None = None,
        job_id: int | None = None,
        source: str | None = None,
        limit: int = 200,
    ) -> dict:
        params: dict[str, object] = {"limit": limit}
        for k, v in (
            ("since", since),
            ("project", project),
            ("event", event),
            ("job_id", job_id),
            ("source", source),
        ):
            if v is not None:
                params[k] = v
        return {"events": self._request("GET", "/events", params=params).json()}

    def workers(self) -> dict:
        return self._request("GET", "/workers").json()

    def delete_worker(self, host: str) -> dict:
        return self._request("DELETE", f"/workers/{host}").json()

    # NOTE: no `job_get`. It was a second name for `status()` — same GET
    # /jobs/{id}, same response — and having two spellings of one call is what
    # let the MCP layer grow a duplicate `jobd_job_get` tool. Use status().

    # Low-level passthrough helpers so callers using c.get()/c.post() patterns
    # still route through _request() for error translation.
    def get(self, path: str, *, params: dict | None = None) -> httpx.Response:
        return self._request("GET", path, params=params)

    def post(self, path: str, *, json: object = None, params: dict | None = None) -> httpx.Response:
        return self._request("POST", path, json=json, params=params)

    def delete(self, path: str) -> httpx.Response:
        return self._request("DELETE", path)

    def stream(self, method: str, path: str, *, timeout: float | None = None):
        """Stream a long-lived response (e.g. SSE from /wait/{id}). Delegates
        to httpx.Client.stream on the shared client so the Bearer header
        injected at __init__ propagates. Pass timeout=None for no read
        timeout (the /wait endpoint may idle for hours)."""
        return self._client.stream(method, f"{self.base_url}{path}", timeout=timeout)

    def __enter__(self) -> JobdClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._client.close()


# ---------------------------------------------------------------------------
# Workload-side preemption protocol (path B / #8). User training scripts call
# install_preemption_handler() to opt in. The worker drives this via SIGTERM
# during a preempt, expecting the user to checkpoint then print the
# `jobd-checkpoint-complete` sentinel before exiting.
# ---------------------------------------------------------------------------

CHECKPOINT_COMPLETE_TOKEN = "jobd-checkpoint-complete"

_preempt_state: dict = {"sigterm_at": None, "grace_s": None, "fn": None}


def install_preemption_handler(checkpoint_fn: Callable[[float], None]) -> None:
    """Install a SIGTERM handler that calls `checkpoint_fn(time_remaining)`,
    prints the `jobd-checkpoint-complete` sentinel on success, and exits.

    Convention: when jobd preempts a running job the worker sends SIGTERM
    and waits up to `JOBD_CHECKPOINT_GRACE_S` seconds (set by the worker;
    default 60s) before SIGKILL. `checkpoint_fn` is the user's hook to
    durably save state; it receives the remaining grace as a float so it
    can self-cap. Exceptions are logged and the process exits 1 without
    printing the sentinel — the broker won't fire the
    `checkpoint_complete` observability event for failed checkpoints.

    `checkpoint_fn` runs inside the SIGTERM handler, so it must use only
    async-signal-safe operations: prefer writing state to the
    `JOBD_CHECKPOINT_DIR` directory, and use `os.write(1, ...)` rather than
    buffered `print()` for any logging (buffered I/O is not reentrant and can
    raise "reentrant call" if SIGTERM interrupts the main thread mid-write).

    No-op outside a jobd context: if `JOBD_CHECKPOINT_GRACE_S` is unset
    the handler still installs (so test scripts work) and reports a 60s
    grace.
    """
    grace_raw = os.environ.get("JOBD_CHECKPOINT_GRACE_S")
    try:
        grace_s = float(grace_raw) if grace_raw else 60.0
    except ValueError:
        grace_s = 60.0
    _preempt_state["grace_s"] = grace_s
    _preempt_state["sigterm_at"] = None
    _preempt_state["fn"] = checkpoint_fn

    def _handler(_signum, _frame):
        # This runs in signal-handler context, so it must use ONLY
        # async-signal-safe operations. Buffered I/O (print) is NOT reentrant:
        # if SIGTERM interrupts the main thread while it holds the stdio buffer
        # lock, print() here raises "RuntimeError: reentrant call". Write to the
        # raw fd instead, and exit via os._exit (skips the atexit buffer-flush
        # that could itself reentrant-fail). checkpoint_fn is user code that
        # runs in this same context — it should likewise avoid buffered stdio
        # (write to the JOBD_CHECKPOINT_DIR, or use os.write) for the same
        # reason. The sentinel below is written directly to stdout (fd 1), where
        # the worker scans for it byte-for-byte.
        _preempt_state["sigterm_at"] = _time.monotonic()
        try:
            checkpoint_fn(time_remaining())
        except Exception as e:  # noqa: BLE001 — surface anything from user code
            os.write(2, f"[jobd] checkpoint_fn raised: {e}\n".encode())
            os._exit(1)
        os.write(1, (CHECKPOINT_COMPLETE_TOKEN + "\n").encode())
        os._exit(0)

    _signal.signal(_signal.SIGTERM, _handler)


def time_remaining() -> float:
    """Seconds until the worker SIGKILLs this process during a preempt.

    Before SIGTERM lands: returns the full grace value (no decay yet).
    Inside the user's `checkpoint_fn`: returns `grace_s - elapsed`.
    Floors at 0; never returns negative.
    """
    grace_s = _preempt_state.get("grace_s")
    if grace_s is None:
        grace_s = 60.0
    sigterm_at = _preempt_state.get("sigterm_at")
    if sigterm_at is None:
        return float(grace_s)
    return max(0.0, float(grace_s) - (_time.monotonic() - float(sigterm_at)))
