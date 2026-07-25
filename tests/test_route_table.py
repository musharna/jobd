"""The enumeration the auth and MCP-parity guards are built on must not go quiet.

Both guards compare `live_routes - expected`, which is empty — and therefore
PASSES — when `live_routes` is empty. FastAPI 0.140 made exactly that happen by
changing `include_router` to leave an opaque `_IncludedRouter` in `app.routes`
instead of flattening the endpoints. The suite reported one loud failure and two
silent degradations. These tests pin the enumeration itself so the next upstream
change to route plumbing fails here, in one obvious place.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from tests.route_table import broker_route_set


def _app_with_included_router(prefix: str = "") -> FastAPI:
    app = FastAPI()
    router = APIRouter()

    @router.get("/thing")
    def _get_thing():  # pragma: no cover - never called, only enumerated
        return {}

    @router.post("/thing/{ident}")
    def _post_thing(ident: str):  # pragma: no cover
        return {}

    app.include_router(router, prefix=prefix) if prefix else app.include_router(router)
    return app


def test_endpoints_behind_include_router_are_found():
    """The regression itself: every broker endpoint lives behind a router."""
    routes = broker_route_set(_app_with_included_router())
    assert "GET /thing" in routes
    assert "POST /thing/{ident}" in routes


def test_include_prefix_is_resolved_not_dropped():
    """`original_router.routes` would report the PRE-include path here, which is
    why this reads the OpenAPI schema instead."""
    routes = broker_route_set(_app_with_included_router(prefix="/v2"))
    assert "GET /v2/thing" in routes
    assert "GET /thing" not in routes


def test_directly_registered_routes_still_appear():
    app = FastAPI()

    @app.get("/direct")
    def _direct():  # pragma: no cover
        return {}

    assert "GET /direct" in broker_route_set(app)


def test_docs_and_schema_endpoints_are_excluded():
    routes = broker_route_set(_app_with_included_router())
    assert not [r for r in routes if "/openapi" in r or "/docs" in r or "/redoc" in r]


def test_the_real_broker_exposes_a_substantial_route_table(
    tmp_path, sample_projects_yaml, sample_profiles_yaml, sample_classifier_yaml
):
    """The floor that makes 'silently empty' impossible to miss. The broker had
    ~30 routes when this landed; 15 leaves room to delete a few without
    tripping, and no room to enumerate nothing."""
    from jobd.app import build_app

    app = build_app(
        db_url=f"sqlite:///{tmp_path}/rt.db",
        projects_path=sample_projects_yaml,
        profiles_path=sample_profiles_yaml,
        classifier_path=sample_classifier_yaml,
        logs_path=tmp_path / "logs",
    )
    routes = broker_route_set(app)
    assert len(routes) >= 15, f"only {len(routes)} routes enumerated: {sorted(routes)}"
    # Spot-check one endpoint from each router module the Stage-3 split created.
    for expected in ("GET /livez", "POST /submit", "GET /workers", "POST /events"):
        assert expected in routes, f"{expected} missing from {sorted(routes)}"
