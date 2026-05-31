"""Verify token + ACL are wired into the real build_app(...)."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_with_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBD_API_TOKEN", "s3cret")
    monkeypatch.delenv("JOBD_ALLOW_NO_AUTH", raising=False)
    monkeypatch.delenv("JOBD_DISABLE_TAILNET_ACL", raising=False)
    from jobd.app import build_app

    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "projects.yaml").write_text("projects: {}\ndefaults: {priority: 40}\n")
    (cfg / "profiles.yaml").write_text("profiles: {}\n")
    (cfg / "classifier.yaml").write_text("rules: []\n")
    db_path = tmp_path / "jobd.db"
    return build_app(
        db_url=f"sqlite:///{db_path}",
        projects_path=cfg / "projects.yaml",
        profiles_path=cfg / "profiles.yaml",
        classifier_path=cfg / "classifier.yaml",
        logs_path=tmp_path / "logs",
    )


def test_request_without_token_is_rejected(app_with_auth):
    client = TestClient(app_with_auth)
    r = client.get("/workers")
    assert r.status_code == 401


def test_request_with_correct_token_is_accepted(app_with_auth):
    client = TestClient(app_with_auth)
    r = client.get("/workers", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200


def test_request_with_wrong_token_is_rejected(app_with_auth):
    client = TestClient(app_with_auth)
    r = client.get("/workers", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
