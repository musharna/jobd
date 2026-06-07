"""Tests for the /resolve dry-run endpoint and the CLI --explain flag.

Covers docs/plans/projects-yaml.md §7 ``test_explain.py`` test list 1-4.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from typer.testing import CliRunner

from jobd.app import build_app
from jobd.db import Job


@pytest.fixture
def projects_with_agrigen(tmp_path):
    path = tmp_path / "projects.yaml"
    path.write_text(
        """projects:
  project-c:
    priority: 55
    defaults:
      max_wall_s: 14400
      idle_timeout_s: 1800
      host_pin: desktop
      requires:
        gpu: true
        needs: [cuda]
      preemptible: true
  _default:
    priority: 40
"""
    )
    return path


@pytest.fixture
def client_with_defaults(
    tmp_path,
    projects_with_agrigen,
    sample_profiles_yaml,
    sample_classifier_yaml,
):
    app = build_app(
        db_url=f"sqlite:///{tmp_path}/jobd.db",
        projects_path=projects_with_agrigen,
        profiles_path=sample_profiles_yaml,
        classifier_path=sample_classifier_yaml,
        logs_path=tmp_path / "logs",
    )
    return TestClient(app)


def test_explain_returns_resolved_body_without_job(client_with_defaults):
    """POST /resolve returns expected sources; no Job row is created."""
    r = client_with_defaults.post(
        "/resolve",
        json={
            "cmd": ["./run.sh"],
            "cwd": "/tmp",
            "project": "project-c",
            "host_pin": "any",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project"] == "project-c"
    assert body["effective_host_pin"]["value"] == "desktop"
    assert body["effective_host_pin"]["source"] == "project_default"
    assert body["effective_max_wall_s"]["value"] == 14400
    assert body["effective_max_wall_s"]["source"] == "project_default"
    assert body["effective_preemptible"]["value"] is True
    assert body["effective_preemptible"]["source"] == "project_default"
    assert body["effective_requires"]["value"]["gpu"] is True
    assert body["effective_requires"]["value"]["needs"] == ["cuda"]
    assert body["submit_warning"] is None

    # Confirm no Job row was created.
    from sqlalchemy.orm import Session

    from jobd import app as app_mod

    engine = app_mod._engine_for_testing()
    with Session(engine) as s:
        rows = s.execute(select(Job)).scalars().all()
        assert rows == []


def test_explain_cli_flag_wins(client_with_defaults):
    """Explicit max_wall_s on the request reports source=cli."""
    r = client_with_defaults.post(
        "/resolve",
        json={
            "cmd": ["./run.sh"],
            "cwd": "/tmp",
            "project": "project-c",
            "max_wall_s": 999,
            "host_pin": "laptop",
            "preemptible": False,
        },
    )
    body = r.json()
    assert body["effective_max_wall_s"]["value"] == 999
    assert body["effective_max_wall_s"]["source"] == "cli"
    assert body["effective_host_pin"]["value"] == "laptop"
    assert body["effective_host_pin"]["source"] == "cli"
    assert body["effective_preemptible"]["value"] is False
    assert body["effective_preemptible"]["source"] == "cli"


def test_explain_global_fallback(client_with_defaults):
    """Unknown project: every source falls through to global; warning fires."""
    r = client_with_defaults.post(
        "/resolve",
        json={
            "cmd": ["./run.sh"],
            "cwd": "/tmp",
            "project": "experimental-new-thing",
            "host_pin": "any",
        },
    )
    body = r.json()
    assert body["effective_max_wall_s"]["source"] == "global"
    assert body["effective_max_wall_s"]["value"] is None
    assert body["effective_idle_timeout_s"]["source"] == "global"
    assert body["effective_host_pin"]["source"] == "global"
    assert body["effective_preemptible"]["value"] is False
    assert body["effective_requires"]["source"] == "global"
    assert body["submit_warning"] is not None
    assert "no entry in projects.yaml" in body["submit_warning"]


def test_explain_output_format(client_with_defaults, monkeypatch):
    """`job submit --project project-c --explain` stdout contains the expected
    header and source annotations.

    The CLI POSTs to /resolve via JobdClient — we stub cli_mod._client() to
    return a context manager that delegates to the in-process TestClient
    instead of hitting localhost.
    """
    import job_cli.cli as cli_mod

    real_client = client_with_defaults

    class _ExplainFakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            pass

        def post(self, path, *, json=None, params=None):
            assert path == "/resolve"
            return real_client.post("/resolve", json=json)

    monkeypatch.setattr(cli_mod, "_client", lambda: _ExplainFakeClient())
    runner = CliRunner()
    r = runner.invoke(
        cli_mod.app,
        [
            "submit",
            "--project",
            "project-c",
            "--explain",
            "--",
            "./run.sh",
        ],
    )
    assert r.exit_code == 0, r.output
    assert "resolved config for project project-c" in r.output
    assert "host_pin" in r.output
    assert "desktop" in r.output
    assert "[source: project default]" in r.output
    assert "max_wall_s" in r.output
