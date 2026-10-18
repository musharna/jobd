"""A loopback HTTP proxy in front of the live broker that only lets reads through.

Every arm talks to the broker through this proxy, never directly: ``run.py``
sets ``JOBD_URL`` to it and removes ``JOBD_API_TOKEN`` from the arm's
environment. The proxy adds the token itself, so a request that did not pass
through ``allowed`` cannot authenticate (the broker answers 401 to a
tokenless request).

Allowed:
- ``GET`` and ``HEAD`` on any path except ``/gpu-holders`` (that one runs a
  probe on the broker host).
- ``POST /submit`` only when the JSON body has ``"dry_run": true``: the broker
  returns its routing plan before inserting a row or emitting an event
  (``jobd.broker.submit``, the ``if req.dry_run:`` branch).
- ``POST /resolve`` and ``POST /classify``: they compute and return, nothing
  else (``jobd.broker.routes.config``).

Everything else gets a 403 with the reason and never reaches the broker:
cancel, preempt, adopt, worker delete, heartbeats, events, priority writes,
reloads. Each decision is appended to ``log`` so a run can show what an arm
tried.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

READ_POSTS = {"/resolve", "/classify"}
DENIED_GETS = {"/gpu-holders"}
_HOP = {"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding"}


def allowed(method: str, target: str, body: bytes) -> tuple[bool, str]:
    path = urlsplit(target).path.rstrip("/") or "/"
    if method in ("GET", "HEAD"):
        if path in DENIED_GETS:
            return False, f"{method} {path} runs a probe on the broker host"
        return True, "read"
    if method == "POST" and path in READ_POSTS:
        return True, "computes only"
    if method == "POST" and path == "/submit":
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return False, "POST /submit with a body that is not JSON"
        if isinstance(payload, dict) and payload.get("dry_run") is True:
            return True, "dry-run submit"
        return False, "POST /submit without dry_run=true would enqueue a job"
    return False, f"{method} {path} changes broker state"


class ReadOnlyProxy:
    """``with ReadOnlyProxy(upstream, token) as p: p.url`` -> a loopback URL."""

    def __init__(self, upstream: str, token: str, log: Path | None = None) -> None:
        self.upstream = upstream.rstrip("/")
        self.log = log
        self._lock = threading.Lock()
        self._http = httpx.Client(
            timeout=httpx.Timeout(connect=10, read=300, write=30, pool=30),
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:  # quiet: decisions go to `log`
                pass

            def _handle(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                ok, why = allowed(self.command, self.path, body)
                proxy._record(self.command, self.path, ok, why)
                if not ok:
                    msg = json.dumps(
                        {"detail": f"refused by the benchmark's read-only proxy: {why}"}
                    ).encode()
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(msg)))
                    self.end_headers()
                    self.wfile.write(msg)
                    return
                headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() not in _HOP | {"host", "authorization"}
                }
                try:
                    with proxy._http.stream(
                        self.command, proxy.upstream + self.path, content=body, headers=headers
                    ) as r:
                        self.send_response(r.status_code)
                        for k, v in r.headers.items():
                            if k.lower() not in _HOP:
                                self.send_header(k, v)
                        # Streamed through (GET /wait is server-sent events), so
                        # the length is unknown: close delimits the body.
                        self.send_header("Connection", "close")
                        self.end_headers()
                        for chunk in r.iter_raw():
                            self.wfile.write(chunk)
                            self.wfile.flush()
                except httpx.HTTPError as e:
                    msg = json.dumps({"detail": f"proxy could not reach the broker: {e}"}).encode()
                    self.send_response(502)
                    self.send_header("Content-Length", str(len(msg)))
                    self.end_headers()
                    self.wfile.write(msg)
                self.close_connection = True

            do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _record(self, method: str, path: str, ok: bool, why: str) -> None:
        if self.log is None:
            return
        with self._lock, self.log.open("a") as f:
            f.write(json.dumps({"method": method, "path": path, "allowed": ok, "why": why}) + "\n")

    def __enter__(self) -> ReadOnlyProxy:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._http.close()
