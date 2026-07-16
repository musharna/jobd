"""Health/readiness probes: /health, /livez, /readyz.

Stage-3 split (backlog 2026-07-15): endpoint bodies are VERBATIM from
app.py's build_app — build_router unpacks BrokerDeps into the same local
names the closures always captured, so the move is byte-identical at the
body level and the whole suite passes unchanged.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from sqlalchemy import text

from jobd import __version__
from jobd.broker.context import BrokerDeps


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
        """
        try:
            with SessionLocal() as session:
                session.execute(text("SELECT 1"))
        except Exception as exc:
            # 503, not 500: "not ready" is a retryable state, and it is what every
            # monitor and orchestrator expects from a readiness probe.
            response.status_code = 503
            return {"status": "not_ready", "reason": f"{type(exc).__name__}: {exc}"[:200]}
        return {"status": "ready"}

    return router
