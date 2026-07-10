"""LOW-sec (audit 2026-07-10): interactive docs + schema routes are disabled.

FastAPI's /docs, /redoc, /openapi.json are Starlette-mounted and bypass the
app-level require_token dependency, letting a tokenless tailnet peer enumerate
the API. build_app passes docs_url=None/redoc_url=None/openapi_url=None; this
pins that they stay off (404).
"""

import pytest
from fastapi.testclient import TestClient

from jobd.app import build_app


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


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_interactive_docs_routes_disabled(client, path):
    assert client.get(path).status_code == 404
