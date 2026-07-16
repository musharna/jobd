"""LOW-sec (audit 2026-07-10): interactive docs + schema routes are disabled.

FastAPI's /docs, /redoc, /openapi.json are Starlette-mounted and bypass the
app-level require_token dependency, letting a tokenless tailnet peer enumerate
the API. build_app passes docs_url=None/redoc_url=None/openapi_url=None; this
pins that they stay off (404).
"""

import pytest


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_interactive_docs_routes_disabled(client, path):
    assert client.get(path).status_code == 404
