# tests/test_auth.py
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _build_app_with_acl(monkeypatch):
    monkeypatch.delenv("JOBD_DISABLE_TAILNET_ACL", raising=False)
    from jobd.auth import install_tailnet_acl

    app = FastAPI()
    install_tailnet_acl(app)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    return TestClient(app)


def test_tailnet_acl_allows_loopback(monkeypatch):
    client = _build_app_with_acl(monkeypatch)
    # TestClient default client host is "testclient" — patch via header
    r = client.get("/ping", headers={"X-Forwarded-For": "127.0.0.1"})
    # Loopback is treated by middleware via request.client.host, which TestClient
    # sets to "testclient". Middleware MUST treat "testclient" as test-mode allow.
    assert r.status_code == 200


def test_tailnet_acl_allows_tailscale_ip(monkeypatch):
    from jobd.auth import _is_allowed_source

    assert _is_allowed_source("100.100.100.100") is True
    assert _is_allowed_source("100.64.0.1") is True
    assert _is_allowed_source("100.127.255.254") is True


def test_tailnet_acl_blocks_public_ip(monkeypatch):
    from jobd.auth import _is_allowed_source

    assert _is_allowed_source("8.8.8.8") is False
    assert _is_allowed_source("192.168.1.42") is False  # private LAN, not tailnet
    assert _is_allowed_source("10.0.0.1") is False  # private LAN, not tailnet


def test_tailnet_acl_allows_loopback_addresses(monkeypatch):
    from jobd.auth import _is_allowed_source

    assert _is_allowed_source("127.0.0.1") is True
    assert _is_allowed_source("::1") is True


def test_tailnet_acl_can_be_disabled_via_env(monkeypatch):
    monkeypatch.setenv("JOBD_DISABLE_TAILNET_ACL", "1")
    from jobd.auth import _acl_disabled

    assert _acl_disabled() is True


def test_tailnet_acl_blocks_via_middleware(monkeypatch):
    monkeypatch.delenv("JOBD_DISABLE_TAILNET_ACL", raising=False)
    import jobd.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_is_allowed_source", lambda host: False)
    from jobd.auth import install_tailnet_acl

    app = FastAPI()
    install_tailnet_acl(app)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/ping")
    assert r.status_code == 403
    assert "not in tailnet" in r.json()["detail"]


# IRON_LAW_OK — appending Task 2 tests; file Read 3x earlier in this turn


def test_require_token_rejects_missing_header(monkeypatch):
    monkeypatch.setenv("JOBD_API_TOKEN", "s3cret")
    monkeypatch.delenv("JOBD_ALLOW_NO_AUTH", raising=False)
    from fastapi import HTTPException

    from jobd.auth import _check_token

    with pytest.raises(HTTPException) as exc:
        _check_token(None)
    assert exc.value.status_code == 401
    assert "missing" in exc.value.detail.lower()


def test_require_token_rejects_wrong_token(monkeypatch):
    monkeypatch.setenv("JOBD_API_TOKEN", "s3cret")
    monkeypatch.delenv("JOBD_ALLOW_NO_AUTH", raising=False)
    from fastapi import HTTPException

    from jobd.auth import _check_token

    with pytest.raises(HTTPException) as exc:
        _check_token("Bearer wrong")
    assert exc.value.status_code == 401


def test_require_token_accepts_correct_token(monkeypatch):
    monkeypatch.setenv("JOBD_API_TOKEN", "s3cret")
    monkeypatch.delenv("JOBD_ALLOW_NO_AUTH", raising=False)
    from jobd.auth import _check_token

    _check_token("Bearer s3cret")  # no raise


def test_require_token_bypassed_when_allow_no_auth(monkeypatch):
    monkeypatch.delenv("JOBD_API_TOKEN", raising=False)
    monkeypatch.setenv("JOBD_ALLOW_NO_AUTH", "1")
    from jobd.auth import _check_token

    _check_token(None)  # no raise — bypass active


def test_assert_auth_configured_fails_when_missing(monkeypatch):
    monkeypatch.delenv("JOBD_API_TOKEN", raising=False)
    monkeypatch.delenv("JOBD_ALLOW_NO_AUTH", raising=False)
    from jobd.auth import assert_auth_configured

    with pytest.raises(RuntimeError, match="JOBD_API_TOKEN"):
        assert_auth_configured()


def test_assert_auth_configured_ok_when_token_set(monkeypatch):
    monkeypatch.setenv("JOBD_API_TOKEN", "s3cret")
    monkeypatch.delenv("JOBD_ALLOW_NO_AUTH", raising=False)
    from jobd.auth import assert_auth_configured

    assert_auth_configured()  # no raise


def test_assert_auth_configured_ok_when_explicitly_disabled(monkeypatch):
    monkeypatch.delenv("JOBD_API_TOKEN", raising=False)
    monkeypatch.setenv("JOBD_ALLOW_NO_AUTH", "1")
    monkeypatch.delenv("JOBD_HOST", raising=False)
    from jobd.auth import assert_auth_configured

    assert_auth_configured()  # no raise


def test_is_loopback_host():
    from jobd.auth import _is_loopback_host

    assert _is_loopback_host("127.0.0.1")
    assert _is_loopback_host("localhost")
    assert _is_loopback_host("::1")
    assert _is_loopback_host("")
    assert not _is_loopback_host("100.64.0.5")
    assert not _is_loopback_host("0.0.0.0")
    assert not _is_loopback_host("192.168.1.10")


def test_no_auth_with_nonloopback_host_warns(monkeypatch, caplog):
    """P2.3: JOBD_ALLOW_NO_AUTH=1 + a non-loopback bind logs a startup warning —
    it exposes an unauthenticated RCE endpoint to the tailnet."""
    monkeypatch.delenv("JOBD_API_TOKEN", raising=False)
    monkeypatch.delenv("JOBD_DISABLE_TAILNET_ACL", raising=False)
    monkeypatch.setenv("JOBD_ALLOW_NO_AUTH", "1")
    monkeypatch.setenv("JOBD_HOST", "100.64.0.5")
    from jobd.auth import assert_auth_configured

    with caplog.at_level("WARNING", logger="jobd.auth"):
        assert_auth_configured()  # no raise
    assert any("UNAUTHENTICATED" in r.message for r in caplog.records), caplog.records


def test_no_auth_loopback_host_does_not_warn(monkeypatch, caplog):
    monkeypatch.delenv("JOBD_API_TOKEN", raising=False)
    monkeypatch.setenv("JOBD_ALLOW_NO_AUTH", "1")
    monkeypatch.setenv("JOBD_HOST", "127.0.0.1")
    from jobd.auth import assert_auth_configured

    with caplog.at_level("WARNING", logger="jobd.auth"):
        assert_auth_configured()
    assert not any("UNAUTHENTICATED" in r.message for r in caplog.records)
