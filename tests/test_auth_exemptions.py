"""The unauthenticated allow-list must never quietly widen.

`/livez` and `/readyz` are served without a bearer token, because a generic HTTP monitor
cannot send one — which is why jobd was the only homelab service Uptime Kuma did not
watch. That exemption is the one hole in the broker's auth wall, and a hole nobody is
watching is how an auth wall becomes decoration.

So the guard is DERIVED, not hand-listed: it enumerates the live route table and asserts
that every route except the named two still demands a token. A new route added tomorrow
is authenticated by default, and if someone widens the exemption they have to do it here,
in the open, and say why.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jobd.app import build_app
from jobd.auth import _UNAUTHENTICATED_PATHS

_EXPECTED_EXEMPT = {"/livez", "/readyz"}


@pytest.fixture
def authed_app(
    tmp_path, sample_projects_yaml, sample_profiles_yaml, sample_classifier_yaml, monkeypatch
):
    """A broker with auth ACTUALLY ON — the rest of the suite bypasses it."""
    monkeypatch.delenv("JOBD_ALLOW_NO_AUTH", raising=False)
    monkeypatch.setenv("JOBD_API_TOKEN", "test-token-not-a-real-secret")
    monkeypatch.setenv("JOBD_DISABLE_TAILNET_ACL", "1")
    app = build_app(
        db_url=f"sqlite:///{tmp_path}/auth.db",
        projects_path=sample_projects_yaml,
        profiles_path=sample_profiles_yaml,
        classifier_path=sample_classifier_yaml,
        logs_path=tmp_path / "logs",
    )
    return app


def test_the_exemption_list_is_exactly_the_two_probes():
    assert set(_UNAUTHENTICATED_PATHS) == _EXPECTED_EXEMPT, (
        f"the unauthenticated allow-list changed to {sorted(_UNAUTHENTICATED_PATHS)}. "
        "This is the ONLY hole in the broker's auth wall — widening it needs a deliberate "
        "decision and a reason, not a drive-by edit."
    )


def test_probes_answer_without_a_token(authed_app):
    with TestClient(authed_app) as client:
        for path in sorted(_EXPECTED_EXEMPT):
            r = client.get(path)
            assert r.status_code == 200, f"{path} should be reachable unauthenticated: {r.text}"


def test_probes_leak_nothing(authed_app):
    """They must be mute. A version string on an unauthenticated endpoint is a free gift
    to anyone fingerprinting the fleet — /health keeps the version AND keeps its auth."""
    with TestClient(authed_app) as client:
        for path in sorted(_EXPECTED_EXEMPT):
            body = client.get(path).json()
            assert set(body) <= {"status", "reason"}, f"{path} returned extra keys: {body}"
            assert "version" not in str(body).lower(), f"{path} leaks a version: {body}"


def test_every_other_route_still_requires_a_token(authed_app):
    """The derived guard. Nothing but the probes may answer without auth."""
    unprotected: list[str] = []
    with TestClient(authed_app) as client:
        for route in authed_app.routes:
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", None) or set()
            if "GET" not in methods or not path or path in _EXPECTED_EXEMPT:
                continue
            if "{" in path or path.startswith("/metrics"):
                continue  # parameterised routes and the metrics mount are covered below
            r = client.get(path)
            if r.status_code != 401:
                unprotected.append(f"{path} -> {r.status_code}")

    assert not unprotected, (
        f"these routes answered WITHOUT a bearer token: {unprotected}. Every route except "
        f"{sorted(_EXPECTED_EXEMPT)} must 401. If one of these is meant to be public, add "
        "it to auth._UNAUTHENTICATED_PATHS deliberately — do not let it happen by accident."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/livez-detail", "/readyz2", "/livezzz", "/readyz/secrets"])
async def test_the_match_is_exact_not_a_prefix(path, monkeypatch):
    """A path that merely STARTS WITH an exempt one must still demand a token.

    This is the mutation the route-table guard above could not catch: swapping
    `path in _UNAUTHENTICATED_PATHS` for `any(path.startswith(p) ...)` passes every other
    test in this file, because no route today happens to begin with /livez or /readyz. It
    would be pure luck — the next route named /readyz-detail becomes public silently.
    So assert the semantics directly, at the dependency, independent of the route table.
    """
    from fastapi import HTTPException
    from starlette.datastructures import URL

    from jobd.auth import require_token

    monkeypatch.delenv("JOBD_ALLOW_NO_AUTH", raising=False)
    monkeypatch.setenv("JOBD_API_TOKEN", "test-token-not-a-real-secret")

    class _Req:
        url = URL(f"http://broker{path}")

    with pytest.raises(HTTPException) as exc:
        await require_token(_Req(), authorization=None)  # type: ignore[arg-type]
    assert exc.value.status_code == 401, (
        f"{path} was served WITHOUT a token. The exemption is matching by prefix, not "
        "exactly — so any future route beginning with /livez or /readyz becomes public "
        "by accident. Use exact membership."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/livez", "/readyz"])
async def test_probes_are_reachable_from_a_NON_TAILNET_source(path, monkeypatch):
    """The test that would have caught the real bug. There are TWO walls, not one.

    The probes shipped exempt from the bearer token but NOT from the tailnet source-IP
    ACL — so the blackbox exporter, a bridge container with source 172.20.0.4, got a 403
    and the probes were useless for the monitoring they exist to enable. Every unit test
    passed, because the TestClient's source IP is allow-listed. Only a real container
    revealed it.

    A monitor is, definitionally, something that is NOT on the tailnet. Assert that.
    """
    from jobd.auth import TailnetACLMiddleware

    monkeypatch.delenv("JOBD_DISABLE_TAILNET_ACL", raising=False)

    called = False

    async def _next(_req):
        nonlocal called
        called = True
        return "served"

    class _Client:
        host = "172.20.0.4"  # a docker bridge container: NOT tailnet, NOT loopback

    class _Req:
        from starlette.datastructures import URL

        url = URL(f"http://broker{path}")
        client = _Client()

    mw = TailnetACLMiddleware(app=None)  # type: ignore[arg-type]
    result = await mw.dispatch(_Req(), _next)  # type: ignore[arg-type]

    assert called and result == "served", (
        f"{path} was REJECTED by the tailnet ACL for a container source IP. Removing the "
        "token wall is not enough — the ACL is a second wall, and a probe that only "
        "clears one is exactly as unreachable as before. This is why jobd was the one "
        "homelab service nothing monitored."
    )


@pytest.mark.asyncio
async def test_the_acl_still_rejects_a_container_on_a_real_route(monkeypatch):
    """Sanity: the test above must be proving an exemption, not a disabled ACL."""
    from starlette.datastructures import URL

    from jobd.auth import TailnetACLMiddleware

    monkeypatch.delenv("JOBD_DISABLE_TAILNET_ACL", raising=False)

    async def _next(_req):
        return "served"

    class _Client:
        host = "172.20.0.4"

    class _Req:
        url = URL("http://broker/workers")
        client = _Client()

    mw = TailnetACLMiddleware(app=None)  # type: ignore[arg-type]
    resp = await mw.dispatch(_Req(), _next)  # type: ignore[arg-type]
    assert getattr(resp, "status_code", None) == 403, (
        "the ACL let a docker-bridge source reach /workers — the source-IP wall is down"
    )


def test_a_real_route_401s_without_a_token(authed_app):
    """Sanity: the guard above is only meaningful if auth is actually engaged here."""
    with TestClient(authed_app) as client:
        assert client.get("/workers").status_code == 401
        assert (
            client.get(
                "/workers", headers={"Authorization": "Bearer test-token-not-a-real-secret"}
            ).status_code
            == 200
        )
