"""POST /submit and POST /resolve must resolve every shared field identically.

/resolve is documented as "dry-run submit", so the config it previews must be
exactly the config /submit persists. Before the Quality-3 dedup the two hand-
encoded the CLI > project_default > profile > global cascade separately and had
already drifted (the profile host_pin branch carried a `!= "any"` guard in
/resolve that /submit lacked). Both now delegate to
``jobd.config.resolve_effective_config``; these tests lock the invariant so any
future re-divergence fails CI.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobd.app import build_app
from jobd.db import Job


@pytest.fixture
def agreement_projects_yaml(tmp_path):
    path = tmp_path / "projects.yaml"
    path.write_text(
        """projects:
  pinned-proj:
    priority: 55
    defaults:
      max_wall_s: 14400
      idle_timeout_s: 1800
      checkpoint_grace_s: 120
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
def client(tmp_path, agreement_projects_yaml, sample_profiles_yaml, sample_classifier_yaml):
    app = build_app(
        db_url=f"sqlite:///{tmp_path}/jobd.db",
        projects_path=agreement_projects_yaml,
        profiles_path=sample_profiles_yaml,
        classifier_path=sample_classifier_yaml,
        logs_path=tmp_path / "logs",
    )
    return TestClient(app)


# (project, profile, extra request fields) -> expected effective host_pin (value, source).
# The host_pin expectation pins the exact behavior of the shared cascade; the
# body-vs-row agreement assertions below cover every other shared field.
CASES = [
    # project default host_pin wins over the "any" sentinel.
    ("pinned-proj", None, {}, ("desktop", "project_default")),
    # explicit CLI host_pin beats the project default.
    ("pinned-proj", None, {"host_pin": "laptop"}, ("laptop", "cli")),
    # project default beats a profile host_hint.
    ("pinned-proj", "gpu-heavy", {}, ("desktop", "project_default")),
    # THE DRIFT CASE: a profile host_hint of "any" is not a real pin, so it must
    # resolve to global/"any" — not be attributed to (or pinned by) the profile.
    ("exp-unknown", "small", {}, ("any", "global")),
    # a real profile host_hint shows through when no project default exists.
    ("exp-unknown", "gpu-heavy", {}, ("desktop", "profile")),
    # explicit CLI wins even for an unknown project + profile.
    ("exp-unknown", "small", {"host_pin": "msi-4080"}, ("msi-4080", "cli")),
]


@pytest.mark.parametrize("project, profile, extra, expected_host_pin", CASES)
def test_submit_matches_resolve(client, project, profile, extra, expected_host_pin):
    body_req = {"cmd": ["./run.sh"], "cwd": "/tmp", "project": project, "host_pin": "any"}
    if profile is not None:
        body_req["profile"] = profile
    body_req.update(extra)

    # /resolve preview
    r = client.post("/resolve", json=body_req)
    assert r.status_code == 200, r.text
    resolved = r.json()

    exp_value, exp_source = expected_host_pin
    assert resolved["effective_host_pin"]["value"] == exp_value
    assert resolved["effective_host_pin"]["source"] == exp_source

    # /submit persists a real Job row
    s = client.post("/submit", json=body_req)
    assert s.status_code == 200, s.text

    engine = client.app.state.engine
    with Session(engine) as sess:
        rows = sess.execute(select(Job)).scalars().all()
        assert len(rows) == 1, f"expected exactly one persisted job, got {len(rows)}"
        job = rows[0]

    # Every shared field the Job persists must equal what /resolve previewed.
    assert job.host_pin == resolved["effective_host_pin"]["value"]
    assert job.priority == resolved["effective_priority"]["value"]
    assert job.preemptible == resolved["effective_preemptible"]["value"]
    assert job.max_wall_s == resolved["effective_max_wall_s"]["value"]
    assert job.idle_timeout_s == resolved["effective_idle_timeout_s"]["value"]
    assert job.checkpoint_grace_s == resolved["effective_checkpoint_grace_s"]["value"]

    # requires: /resolve surfaces the model_dumped dict (or None); /submit stores
    # the same object as a JSON string ("{}" when unset).
    resolved_requires = resolved["effective_requires"]["value"] or {}
    assert json.loads(job.requires_json) == resolved_requires
