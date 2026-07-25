"""Health/readiness probes: /health, /livez, /readyz.

Stage-3 split (backlog 2026-07-15): endpoint bodies are VERBATIM from
app.py's build_app — build_router unpacks BrokerDeps into the same local
names the closures always captured, so the move is byte-identical at the
body level and the whole suite passes unchanged.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response
from sqlalchemy import text

from jobd import __version__
from jobd.broker.context import BrokerDeps

log = logging.getLogger("jobd")

# The ONLY thing /readyz says when the database is unreachable. A fixed string,
# not the exception, because /readyz answers callers who presented no token and
# whose source IP was never checked — see the endpoint docstring.
_NOT_READY_REASON = "database_unavailable"


def build_router(deps: BrokerDeps) -> APIRouter:
    router = APIRouter()
    SessionLocal = deps.session_local

    @router.get("/health")
    def health():
        return {"status": "ok", "version": __version__}

    # --- Unauthenticated probes. ------------------------------------------------
    # Exempted by exact path in auth._UNAUTHENTICATED_PATHS. They exist because a
    # generic HTTP monitor cannot send a bearer token: Uptime Kuma watches twelve
    # homelab services and jobd — the broker the whole fleet depends on — was the only
    # one it did NOT, purely because every route was behind the token wall.
    #
    # Deliberately mute: alive-or-not, ready-or-not, nothing else. No version, no
    # counts, no job data. /health keeps the version and keeps its auth, because the
    # container healthcheck must PROVE it is talking to jobd (scripts/healthcheck.py —
    # a probe that could not tell which daemon answered is a bug we have shipped).
    @router.get("/livez")
    def livez():
        """Process is up. Deliberately does not touch the DB."""
        return {"status": "alive"}

    @router.get("/readyz")
    def readyz(response: Response):
        """The broker can actually SERVE — round-trips a query to SQLite.

        /livez green + /readyz red = "the process is fine, the database is wedged",
        which says fix the DB rather than restart the process. Restarting would have
        destroyed the evidence.

        The `reason` is a fixed CATEGORY, never the exception text (audit
        2026-07-25 S-1). This endpoint is exempt from BOTH walls — the bearer
        token AND the tailnet source-IP ACL, since auth._PUBLIC_PROBE_PATHS is
        consulted by each — so whatever it returns is readable by anyone who
        can reach the port. It used to return `f"{type(exc).__name__}: {exc}"`.
        On the default SQLite backend that leaks little, but JOBD_DB_URL takes
        any SQLAlchemy URL and a network driver puts host, port and username
        into that message. The operator loses nothing: the full exception goes
        to the broker log, which is where a diagnosis belongs.

        Deliberately NOT cached (audit 2026-07-25 S-4): /metrics caches 5s and
        the asymmetry is intentional. A cached readiness probe can answer
        "ready" for the length of its TTL after the database has gone — wrong
        at the only moment the probe matters. Getting that right costs one
        `SELECT 1` per scrape.
        """
        try:
            with SessionLocal() as session:
                session.execute(text("SELECT 1"))
        except Exception as exc:
            # 503, not 500: "not ready" is a retryable state, and it is what every
            # monitor and orchestrator expects from a readiness probe.
            log.warning("readyz: database round-trip failed: %s: %s", type(exc).__name__, exc)
            response.status_code = 503
            return {"status": "not_ready", "reason": _NOT_READY_REASON}
        return {"status": "ready"}

    return router
