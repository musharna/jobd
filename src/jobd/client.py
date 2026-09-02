"""HTTP client wrapper for the jobd broker. Shared by job_cli and jobd.mcp.

Also exposes workload-side preemption helpers (`install_preemption_handler`,
`time_remaining`) that user training scripts call to participate in the
broker's preempt + checkpoint protocol.
"""

from __future__ import annotations

import os
import signal as _signal
import socket
import ssl
import time as _time
from collections.abc import Callable, Iterator

import httpx


class BrokerUnreachable(Exception):
    """Network failure — DNS, connect refused, TLS, connect timeout.

    Carries `kind` and `hint` because the operator's next move diverges sharply
    by cause, and the raw exception text does not say which happened. A refused
    connection means the host answered and the broker is down. A connect
    timeout means the packets were dropped, which is a firewall or a tailnet
    ACL far more often than a broker fault.

    On 2026-09-02 that distinction cost a debugging session: `job ping` printed
    a flat "unreachable" for an ACL drop, and a broker that was healthy,
    serving, and freshly upgraded was reported to the user as down.
    """

    def __init__(self, message: str, *, kind: str = "network", hint: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.hint = hint


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


# kind -> what the network just told you. Phrased as an observation the
# operator can act on, not as generic "check your connection" advice.
_UNREACHABLE_HINTS: dict[str, str] = {
    "dns": (
        "the host name did not resolve, so nothing was dialled — check JOBD_URL for a "
        "typo, and whether this machine's resolver (or tailnet MagicDNS) is up"
    ),
    "refused": (
        "the host answered and actively refused the connection, so the network path is "
        "fine and the broker is almost certainly not running — or is listening on a "
        "different port or interface than JOBD_URL names"
    ),
    "timeout": (
        "the connection was dropped rather than refused, which points at a firewall, a "
        "tailnet ACL, or routing — not at the broker. If the host answers ping but this "
        "port times out, the broker is likely healthy and simply unreachable from here"
    ),
    "read_timeout": (
        "the connection was established and then went quiet, so the broker is running "
        "but did not answer in time — wedged, overloaded, or blocked on a slow query"
    ),
    "tls": (
        "the TLS handshake failed — check the scheme in JOBD_URL and the certificate "
        "the broker is serving"
    ),
    # Deliberately empty: this means "we could not tell". Inventing advice here
    # would be worse than saying nothing.
    "network": "",
}


def _cause_chain(exc: BaseException, *, depth: int = 8) -> Iterator[BaseException]:
    """Walk __cause__/__context__. httpx wraps the OSError that actually carries
    the errno several layers down, and which layer varies by transport and by
    httpx version — so match on the chain rather than on the top-level type."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and depth > 0 and id(cur) not in seen:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or cur.__context__
        depth -= 1


def classify_connect_failure(exc: BaseException) -> str:
    """Name the network failure behind an httpx transport error.

    Returns a key of `_UNREACHABLE_HINTS`. Type checks first (they are exact),
    then the cause chain (errno-bearing), then message text as a last resort —
    a wrong guess here is worse than `network`, so each fallback is narrower
    than the one before it.
    """
    if isinstance(exc, httpx.ConnectTimeout):
        return "timeout"
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return "read_timeout"

    for e in _cause_chain(exc):
        if isinstance(e, socket.gaierror):
            return "dns"
        if isinstance(e, ConnectionRefusedError):
            return "refused"
        if isinstance(e, ssl.SSLError):
            return "tls"
        if isinstance(e, TimeoutError):
            return "timeout"

    text = str(exc).lower()
    if any(s in text for s in ("name or service not known", "name resolution", "getaddrinfo")):
        return "dns"
    if "connection refused" in text:
        return "refused"
    if "certificate" in text or "ssl" in text:
        return "tls"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    return "network"


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
        # Exposed for callers that must forward the credential somewhere the
        # header can't go (e.g. `job fleet add` writing a worker's env file).
        self.token = _token
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
            kind = classify_connect_failure(e)
            raise BrokerUnreachable(
                f"{type(e).__name__}: {e} (JOBD_URL={self.base_url})",
                kind=kind,
                hint=_UNREACHABLE_HINTS.get(kind, ""),
            ) from e
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

    def list_jobs(
        self,
        *,
        state: str | None = None,
        project: str | None = None,
        limit: int | None = None,
    ) -> dict:
        params: dict[str, str] = {}
        if state:
            params["state_filter"] = state
        if project:
            params["project"] = project
        if limit is not None:
            params["limit"] = str(limit)
        return self._request("GET", "/jobs", params=params).json()

    def list_jobs_with_total(
        self,
        *,
        state: str | None = None,
        project: str | None = None,
        limit: int | None = None,
    ) -> tuple[list, int]:
        """Like list_jobs, but also return the broker's X-Total-Count — the size
        of the FULL filtered set, independent of `limit`. Lets a bounded page
        still report exact counts (audit 2026-07-15 L-8: jobd_list used to fetch
        the entire job history per call to compute them)."""
        params: dict[str, str] = {}
        if state:
            params["state_filter"] = state
        if project:
            params["project"] = project
        if limit is not None:
            params["limit"] = str(limit)
        resp = self._request("GET", "/jobs", params=params)
        rows = resp.json()
        rows = rows if isinstance(rows, list) else rows.get("jobs", [])
        try:
            total = int(resp.headers.get("X-Total-Count", ""))
        except ValueError:
            total = len(rows)  # pre-pagination broker: the page IS the set
        return rows, total

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
