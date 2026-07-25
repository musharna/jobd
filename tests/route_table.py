"""Enumerate a FastAPI app's real endpoints, however deeply they are nested.

Two guards depend on seeing every route the broker actually serves: the MCP
surface-parity map (tests/mcp/test_surface_parity.py) and the auth
"everything but the probes must 401" check (tests/test_auth_exemptions.py).
Both used to do `for r in app.routes` and read `r.path` / `r.methods`.

FastAPI 0.140 broke that assumption. `include_router` no longer flattens the
sub-router's endpoints into `app.routes`; the router is appended as a single
`fastapi.routing._IncludedRouter` whose `path`, `methods` and `routes` are all
absent. Since the Stage-3 split (#69) put EVERY broker endpoint behind an
APIRouter, the shallow loop went from ~30 routes to zero.

That is worse than it sounds. Only one of the three affected assertions
compares in the direction that fails loudly on an empty set; the other two —
including the auth guard — compare `routes - expected`, which is empty when
`routes` is empty. They kept passing while checking nothing. A guard that
silently degrades to vacuous is precisely the failure the parity test's own
message warns about: "a map that describes a broker that isn't there stops
being a guard and becomes decoration."

Why the OpenAPI schema and not the route objects: `_IncludedRouter` does
expose `original_router`, but the routes hanging off it carry their
PRE-INCLUDE paths — `include_router(r, prefix="/v2")` leaves `/thing`, not
`/v2/thing`. `app.openapi()["paths"]` is a documented API and reports final,
prefix-resolved paths. The one thing it omits is `include_in_schema=False`
routes; jobd has none, and adding one would be a deliberate act that should
come with its own guard. The callers additionally assert a non-trivial route
count, so a future upstream change that empties this out fails loudly instead
of going quiet.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

_NON_ENDPOINT_PREFIXES = ("/openapi",)
_NON_ENDPOINT_PATHS = frozenset({"/docs", "/redoc", "/docs/oauth2-redirect"})
_GENERATED_METHODS = frozenset({"HEAD", "OPTIONS"})


def _walk_route_objects(node: Any, seen: set[int]) -> Iterator[str]:
    """Fallback for plain routers / apps without an OpenAPI schema."""
    if id(node) in seen:  # defensive: a cyclic mount would otherwise hang
        return
    seen.add(id(node))
    path = getattr(node, "path", None)
    for method in getattr(node, "methods", None) or ():
        if method not in _GENERATED_METHODS and path:
            yield f"{method} {path}"
    for child in getattr(node, "routes", None) or ():
        yield from _walk_route_objects(child, seen)


def iter_leaf_routes(app: Any) -> Iterator[str]:
    """Yield ``"<METHOD> <path>"`` for every endpoint the app actually serves."""
    schema: dict[str, Any] | None = None
    getter = getattr(app, "openapi", None)
    if callable(getter):
        try:
            result = getter()
            schema = result if isinstance(result, dict) else None
        except Exception:  # pragma: no cover - a broken schema shouldn't blind us
            schema = None

    if schema and schema.get("paths"):
        for path, operations in schema["paths"].items():
            for method in operations:
                m = method.upper()
                if m not in _GENERATED_METHODS:
                    yield f"{m} {path}"
        return

    yield from _walk_route_objects(app, set())


def broker_route_set(app: Any) -> set[str]:
    """`iter_leaf_routes` as a set, minus FastAPI's own docs/schema endpoints."""
    out = set()
    for entry in iter_leaf_routes(app):
        path = entry.split(" ", 1)[1]
        if path in _NON_ENDPOINT_PATHS or path.startswith(_NON_ENDPOINT_PREFIXES):
            continue
        out.add(entry)
    return out
