"""Tests for project-defaults resolution at submit time.

Covers docs/projects-yaml.md §7 test list 1-9. Exercises the broker's
POST /submit handler: CLI flag > project default > profile default >
global. Each test asserts the persisted Job row reflects the right value.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from jobd.app import build_app
from jobd.db import Job


@pytest.fixture
def projects_with_agrigen(tmp_path):
    """projects.yaml with a fully-populated project-c defaults block."""
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
  project-b:
    priority: 80
    defaults:
      host_pin: laptop
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


def _job_row(client, job_id: int) -> Job:
    """Fetch the Job row directly from SQLAlchemy to inspect resolved fields."""

    engine = client.app.state.engine
    from sqlalchemy.orm import Session

    with Session(engine) as s:
        return s.execute(select(Job).where(Job.id == job_id)).scalar_one()


def test_cli_flag_wins_over_project_default(client_with_defaults):
    """req.max_wall_s=7200 with project-c default 14400 → effective 7200."""
    r = client_with_defaults.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-c",
            "host_pin": "any",
            "max_wall_s": 7200,
        },
    )
    assert r.status_code == 200, r.text
    job = _job_row(client_with_defaults, r.json()["id"])
    assert job.max_wall_s == 7200


def test_project_default_wins_over_global(client_with_defaults):
    """req.max_wall_s=None with project-c default 14400 → effective 14400."""
    r = client_with_defaults.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-c",
            "host_pin": "any",
        },
    )
    assert r.status_code == 200, r.text
    job = _job_row(client_with_defaults, r.json()["id"])
    assert job.max_wall_s == 14400
    assert job.idle_timeout_s == 1800


def test_missing_project_falls_through_to_global(client_with_defaults):
    """Unknown project: global defaults applied, warning surfaces."""
    r = client_with_defaults.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "brand-new-experiment",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    job = _job_row(client_with_defaults, body["id"])
    assert job.max_wall_s is None
    assert job.idle_timeout_s is None
    assert job.host_pin == "any"
    assert "no entry in projects.yaml" in (job.warning or "")


def test_host_pin_project_default_overrides_sentinel(client_with_defaults):
    """req.host_pin='any' with project default 'desktop' → row 'desktop'."""
    r = client_with_defaults.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-c",
            "host_pin": "any",
        },
    )
    job = _job_row(client_with_defaults, r.json()["id"])
    assert job.host_pin == "desktop"


def test_host_pin_explicit_cli_not_overridden(client_with_defaults):
    """req.host_pin='laptop' with project default 'desktop' → row 'laptop'."""
    r = client_with_defaults.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-c",
            "host_pin": "laptop",
        },
    )
    job = _job_row(client_with_defaults, r.json()["id"])
    assert job.host_pin == "laptop"


def test_preemptible_sentinel_detects_cli_absence(client_with_defaults):
    """req.preemptible omitted; project default True → row preemptible=True."""
    r = client_with_defaults.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-c",
            "host_pin": "any",
        },
    )
    job = _job_row(client_with_defaults, r.json()["id"])
    assert job.preemptible is True


def test_preemptible_false_explicit_not_overridden(client_with_defaults):
    """req.preemptible=False explicit; project default True → row False."""
    r = client_with_defaults.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-c",
            "host_pin": "any",
            "preemptible": False,
        },
    )
    job = _job_row(client_with_defaults, r.json()["id"])
    assert job.preemptible is False


def test_requires_project_default_applied(client_with_defaults):
    """req.requires=None; project default {gpu:true, needs:[cuda]} →
    requires_json reflects project default."""
    r = client_with_defaults.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-c",
            "host_pin": "any",
        },
    )
    job = _job_row(client_with_defaults, r.json()["id"])
    payload = json.loads(job.requires_json)
    assert payload["gpu"] is True
    assert payload["needs"] == ["cuda"]


def test_project_default_wins_over_profile_for_host_pin(tmp_path, sample_classifier_yaml):
    """Project default 'laptop', profile host_hint 'desktop' → row 'laptop'.

    Requires a profile with a host_hint different from the project default.
    """
    projects = tmp_path / "projects.yaml"
    projects.write_text(
        """projects:
  myproj:
    priority: 50
    defaults:
      host_pin: laptop
  _default: { priority: 40 }
"""
    )
    profiles = tmp_path / "profiles.yaml"
    profiles.write_text(
        """profiles:
  desk-pinned:
    vram_gb: 0
    ram_gb: 1
    cpus: 1
    expected_runtime: 1m
    host_hint: desktop
"""
    )
    app = build_app(
        db_url=f"sqlite:///{tmp_path}/jobd.db",
        projects_path=projects,
        profiles_path=profiles,
        classifier_path=sample_classifier_yaml,
        logs_path=tmp_path / "logs",
    )
    client = TestClient(app)
    r = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "myproj",
            "profile": "desk-pinned",
            "host_pin": "any",
        },
    )
    assert r.status_code == 200, r.text
    job = _job_row(client, r.json()["id"])
    assert job.host_pin == "laptop"
