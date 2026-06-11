# tests/test_auth_hardening.py
import pytest


def test_check_token_strips_trailing_whitespace_in_env(monkeypatch):
    monkeypatch.setenv("JOBD_API_TOKEN", "abc ")
    monkeypatch.delenv("JOBD_ALLOW_NO_AUTH", raising=False)
    from jobd.auth import _check_token

    _check_token("Bearer abc")  # no raise — env token stripped to "abc"


def test_check_token_whitespace_only_token_rejected(monkeypatch):
    monkeypatch.setenv("JOBD_API_TOKEN", "   ")
    monkeypatch.delenv("JOBD_ALLOW_NO_AUTH", raising=False)
    from fastapi import HTTPException

    from jobd.auth import _check_token

    with pytest.raises(HTTPException) as exc:
        _check_token("Bearer abc")
    assert exc.value.status_code == 500


def test_allow_no_auth_plus_disabled_acl_plus_nonloopback_raises(monkeypatch):
    monkeypatch.delenv("JOBD_API_TOKEN", raising=False)
    monkeypatch.setenv("JOBD_ALLOW_NO_AUTH", "1")
    monkeypatch.setenv("JOBD_DISABLE_TAILNET_ACL", "1")
    monkeypatch.setenv("JOBD_HOST", "100.64.0.5")
    from jobd.auth import assert_auth_configured

    with pytest.raises(RuntimeError, match="UNAUTHENTICATED"):
        assert_auth_configured()


def test_allow_no_auth_plus_loopback_still_allowed(monkeypatch):
    monkeypatch.delenv("JOBD_API_TOKEN", raising=False)
    monkeypatch.setenv("JOBD_ALLOW_NO_AUTH", "1")
    monkeypatch.setenv("JOBD_DISABLE_TAILNET_ACL", "1")
    monkeypatch.setenv("JOBD_HOST", "127.0.0.1")
    from jobd.auth import assert_auth_configured

    assert_auth_configured()  # no raise


def test_allow_no_auth_nonloopback_acl_enabled_still_starts(monkeypatch, caplog):
    monkeypatch.delenv("JOBD_API_TOKEN", raising=False)
    monkeypatch.setenv("JOBD_ALLOW_NO_AUTH", "1")
    monkeypatch.delenv("JOBD_DISABLE_TAILNET_ACL", raising=False)
    monkeypatch.setenv("JOBD_HOST", "100.64.0.5")
    from jobd.auth import assert_auth_configured

    with caplog.at_level("WARNING", logger="jobd.auth"):
        assert_auth_configured()  # no raise
    assert any("UNAUTHENTICATED" in r.message for r in caplog.records)


def test_disabled_acl_nonloopback_alone_does_not_raise(monkeypatch, caplog):
    monkeypatch.setenv("JOBD_API_TOKEN", "s3cret")
    monkeypatch.delenv("JOBD_ALLOW_NO_AUTH", raising=False)
    monkeypatch.setenv("JOBD_DISABLE_TAILNET_ACL", "1")
    monkeypatch.setenv("JOBD_HOST", "100.64.0.5")
    from jobd.auth import assert_auth_configured

    with caplog.at_level("WARNING", logger="jobd.auth"):
        assert_auth_configured()  # no raise
    assert any("JOBD_DISABLE_TAILNET_ACL" in r.message for r in caplog.records)
