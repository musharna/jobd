# src/jobd/auth.py
"""Auth + network-ACL helpers for the jobd broker.

Two stacked checks at the broker boundary:
  1. Tailnet-IP source ACL (middleware) — request.client.host must be in
     100.64.0.0/10 or loopback. Defense-in-depth against a stray
     `0.0.0.0` port-publish in docker-compose.yml.
  2. Bearer token dependency — Authorization: Bearer <JOBD_API_TOKEN> must
     match. See `require_token` (Task 2).

Both checks fail-loud: broker refuses to boot if JOBD_API_TOKEN is unset
unless JOBD_ALLOW_NO_AUTH=1 is set explicitly (test/dev escape).
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os

from fastapi import FastAPI, Header, HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# Tailscale's CGNAT range — every device on a tailnet has a 100.64.0.0/10 IP.
_TAILNET = ipaddress.ip_network("100.64.0.0/10")

# TestClient sets request.client.host to the literal "testclient" — treat as
# loopback for unit-test convenience. Production traffic never produces this.
_TEST_CLIENT_HOST = "testclient"


def _acl_disabled() -> bool:
    return os.environ.get("JOBD_DISABLE_TAILNET_ACL", "") == "1"


def _is_allowed_source(host: str) -> bool:
    """Return True if `host` (request.client.host string) should be allowed."""
    if host == _TEST_CLIENT_HOST:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if addr.is_loopback:
        return True
    # ipaddress can't put an IPv6 addr in an IPv4 network — narrow the test.
    return isinstance(addr, ipaddress.IPv4Address) and addr in _TAILNET


class TailnetACLMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if _acl_disabled():
            return await call_next(request)
        client = request.client
        host = client.host if client else ""
        if not _is_allowed_source(host):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": (
                        f"source IP {host!r} not in tailnet (100.64.0.0/10) "
                        "and not loopback; set JOBD_DISABLE_TAILNET_ACL=1 to bypass"
                    )
                },
            )
        return await call_next(request)


def install_tailnet_acl(app: FastAPI) -> None:
    app.add_middleware(TailnetACLMiddleware)


def _token_required() -> bool:
    return os.environ.get("JOBD_ALLOW_NO_AUTH", "") != "1"


def _check_token(authorization: str | None) -> None:
    """Raise HTTPException(401) if Authorization doesn't match JOBD_API_TOKEN.

    No-op if JOBD_ALLOW_NO_AUTH=1.
    """
    if not _token_required():
        return
    expected = os.environ.get("JOBD_API_TOKEN", "").strip()
    if not expected:
        # assert_auth_configured() should have caught this at startup; if we
        # got here in production, fail-closed rather than fail-open.
        raise HTTPException(
            status_code=500,
            detail="JOBD_API_TOKEN unset on broker; refusing all requests",
        )
    if authorization is None:
        raise HTTPException(
            status_code=401,
            detail="missing Authorization: Bearer <token> header",
        )
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization header must be 'Bearer <token>'",
        )
    presented = authorization[len("Bearer ") :]
    # constant-time compare avoids a timing side-channel on the token byte.
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="bad token")


async def require_token(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency. Wire globally via FastAPI(dependencies=[Depends(require_token)])."""
    _check_token(authorization)


def _is_loopback_host(host: str) -> bool:
    """True if `host` only accepts connections from the local machine."""
    h = host.strip().lower()
    return h in ("", "127.0.0.1", "localhost", "::1") or h.startswith("127.")


def assert_auth_configured() -> None:
    """Raise RuntimeError at startup if neither JOBD_API_TOKEN nor JOBD_ALLOW_NO_AUTH=1 is set."""
    host = os.environ.get("JOBD_HOST", "127.0.0.1")
    if os.environ.get("JOBD_ALLOW_NO_AUTH", "") == "1":
        if not _is_loopback_host(host):
            if _acl_disabled():
                raise RuntimeError(
                    "JOBD_ALLOW_NO_AUTH=1 + JOBD_DISABLE_TAILNET_ACL=1 + non-loopback "
                    f"JOBD_HOST={host} would expose an UNAUTHENTICATED broker to the "
                    "network with no access control at all — anyone who can reach it "
                    "can run arbitrary commands on your workers. Refusing to start. "
                    "Set JOBD_API_TOKEN, drop JOBD_DISABLE_TAILNET_ACL, or bind "
                    "JOBD_HOST to 127.0.0.1."
                )
            logging.getLogger("jobd.auth").warning(
                "JOBD_ALLOW_NO_AUTH=1 with non-loopback JOBD_HOST=%s — the broker is "
                "UNAUTHENTICATED and reachable beyond localhost. Anyone who can reach "
                "it can run arbitrary commands on your workers. Set JOBD_API_TOKEN "
                "instead, or bind JOBD_HOST to 127.0.0.1 for a loopback-only broker.",
                host,
            )
        return
    if _acl_disabled() and not _is_loopback_host(host):
        logging.getLogger("jobd.auth").warning(
            "JOBD_DISABLE_TAILNET_ACL=1 with non-loopback JOBD_HOST=%s — the broker's "
            "source-IP ACL is OFF; only the bearer token gates access. Anyone who can "
            "reach the host and holds the token can run arbitrary commands on your "
            "workers. Drop JOBD_DISABLE_TAILNET_ACL or bind JOBD_HOST to 127.0.0.1.",
            host,
        )
    if not os.environ.get("JOBD_API_TOKEN", "").strip():
        raise RuntimeError(
            "JOBD_API_TOKEN env var is required to start the broker. "
            "Set it to a shared secret known to all clients (CLI + workers + "
            "MCP server), OR set JOBD_ALLOW_NO_AUTH=1 to opt into an "
            "unauthenticated broker (NOT recommended outside tests)."
        )
