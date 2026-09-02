"""Shared fixtures for tests/unit/."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from jobd.app import build_app


@pytest.fixture
def logs_dir(tmp_path):
    return tmp_path / "logs"


@pytest.fixture
def rooted_app(tmp_path, sample_profiles_yaml, sample_classifier_yaml):
    """An app whose projects table declares roots.

    Separate from `sample_projects_yaml` on purpose: that fixture's exact
    priorities are asserted across tests/test_api.py, so widening it to carry
    roots would couple this feature's tests to every one of those.
    """
    projects = tmp_path / "rooted-projects.yaml"
    projects.write_text(
        "projects:\n"
        "  beta:\n"
        "    priority: 78\n"
        "    roots: ['/home/user/beta']\n"
        "  gamma:\n"
        "    priority: 60\n"
        "    roots: ['/home/user/gamma']\n"
        "  _default: { priority: 40 }\n"
    )
    return build_app(
        db_url=f"sqlite:///{tmp_path}/rooted.db",
        projects_path=projects,
        profiles_path=sample_profiles_yaml,
        classifier_path=sample_classifier_yaml,
        logs_path=tmp_path / "logs",
    )


@pytest.fixture
def rooted_client(rooted_app):
    return TestClient(rooted_app)


@pytest.fixture
def rooted_logs(rooted_client, tmp_path):
    """(client, logs_dir), mirroring the shared `client_logs` pair."""
    return rooted_client, tmp_path / "logs"
