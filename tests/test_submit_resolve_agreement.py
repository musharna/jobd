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
    ("exp-unknown", "small", {"host_pin": "worker-b"}, ("worker-b", "cli")),
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


# --- spelling-insensitive project identity ------------------------------------
#
# Project names are free text typed at submit time and were matched with a bare
# `name in projects`, so a REGISTERED project silently lost its priority to a
# differently-typed name. Measured on the live broker 2026-08-30: jobs submitted
# as `ARFDSynInt` ran at _default 40 while `arfdsynint` sat deliberately
# registered at 65 — a difference of case alone, with no error raised and no way
# to notice except by reading a warning nothing consumed.


def _submit(client, project: str, **extra):
    r = client.post(
        "/submit",
        json={"project": project, "cmd": ["true"], "cwd": "/tmp", "host_pin": "any", **extra},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _row(client):
    with Session(client.app.state.engine) as sess:
        rows = sess.execute(select(Job)).scalars().all()
    assert len(rows) == 1, f"expected exactly one job row, got {len(rows)}"
    return rows[0]


@pytest.mark.parametrize("typed", ["PINNED-PROJ", "Pinned-Proj", "pinned_proj", "PINNED_PROJ"])
def test_a_differently_spelled_registered_project_keeps_its_priority(client, typed):
    """Case and -/_ variants resolve to the registered project, so the job is
    priced at 55 rather than falling through to _default 40."""
    _submit(client, typed)
    job = _row(client)
    assert job.priority == 55, (
        f"{typed!r} was priced at {job.priority}, not the registered project's 55 — "
        "it fell through to _default"
    )
    # ...and is STORED under the registered spelling, so per-project reporting
    # cannot fragment into one row per spelling.
    assert job.project == "pinned-proj", f"stored as {job.project!r}"


def test_resolve_previews_the_same_resolved_name_submit_stores(client):
    """/resolve is documented as dry-run submit, so it must report the name the
    job will actually run under, not echo back what the caller typed."""
    r = client.post(
        "/resolve",
        json={"project": "PINNED-PROJ", "cmd": ["true"], "cwd": "/tmp", "host_pin": "any"},
    )
    assert r.status_code == 200, r.text
    resolved = r.json()
    assert resolved["project"] == "pinned-proj"
    assert resolved["effective_priority"]["value"] == 55
    assert resolved["submit_warning"] is None

    _submit(client, "PINNED-PROJ")
    assert _row(client).project == resolved["project"]


def test_an_exactly_named_project_is_untouched(client):
    """Positive control. Without it, a resolver that mangled every name into the
    same project would satisfy the assertions above just as well."""
    _submit(client, "pinned-proj")
    job = _row(client)
    assert job.project == "pinned-proj"
    assert job.priority == 55


def test_a_genuinely_new_project_keeps_the_name_its_owner_chose(client):
    """The other positive control: matching nothing must stay untouched. A new
    project has to be able to submit under its own name, still warn, and still
    take _default — folding must not invent a match."""
    body = _submit(client, "Brand-New-Thing")
    job = _row(client)
    assert job.project == "Brand-New-Thing", f"rewrote an unmatched name to {job.project!r}"
    assert job.priority == 40
    assert body["warning"] is not None
    assert "no entry in projects.yaml" in body["warning"]


def test_a_suffix_difference_is_not_treated_as_the_same_project(client):
    """Folding is case and -/_ ONLY, deliberately. `phelipanche` must not be
    folded onto `phelipanche-fm` — that differs by a suffix, and equating them
    would run work at another project's priority, which is the bug this matching
    exists to end, inverted."""
    _submit(client, "pinned")
    job = _row(client)
    assert job.project == "pinned", f"a suffix difference was folded: {job.project!r}"
    assert job.priority == 40
