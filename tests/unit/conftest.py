"""Shared fixtures for tests/unit/."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jobd.app import build_app


@pytest.fixture
def logs_dir(tmp_path):
    return tmp_path / "logs"


@pytest.fixture
def client(tmp_path, sample_projects_yaml, sample_profiles_yaml, sample_classifier_yaml):
    app = build_app(
        db_url=f"sqlite:///{tmp_path}/jobd.db",
        projects_path=sample_projects_yaml,
        profiles_path=sample_profiles_yaml,
        classifier_path=sample_classifier_yaml,
        logs_path=tmp_path / "logs",
    )
    return TestClient(app)
