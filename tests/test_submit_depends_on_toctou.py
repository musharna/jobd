"""H-1 (audit 2026-07-10): submit depends_on TOCTOU → permanent QUEUED strand.

The failed-side reject in /submit is a point read of parent.state. Between that
read and the child INSERT+commit, a parent can reach a failed-side terminal and
run its cascade over the then-current QUEUED set — which does not yet include
the child. The child then commits QUEUED with a terminal-failed parent; the
parent never transitions again so its cascade never re-fires, and
_deps_satisfied needs the (never-reached) COMPLETED → the child strands QUEUED
forever.

The fix re-runs the cascade for each parent AFTER the child is committed and
visible, healing the strand. These tests exercise that post-commit sweep and
guard against false-positive cancellation of live/successful parents.

The race window is simulated deterministically by emptying
``jobd.app._FAILED_SIDE_TERMINAL`` for the child submit (so the point-read
reject is bypassed, exactly as it would be if the read observed the parent
mid-run) while the cascade in ``jobd.broker.state`` keeps the real constant.
"""

import json

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


def _submit(client, depends_on=None, any_exit=False):
    body = {"cmd": ["true"], "cwd": "/tmp", "project": "project-a"}
    if depends_on is not None:
        body["depends_on"] = depends_on
    if any_exit:
        body["depends_on_any_exit"] = True
    return client.post("/submit", json=body)


def _worker_body(host="w1"):
    return {
        "host": host,
        "free_vram_gb": 0,
        "unregistered_vram_gb": 0,
        "free_ram_gb": 8,
        "idle_cpus": 4,
        "arch": "x86_64",
        "os": "linux",
        "gpu": False,
        "tags": [],
        "host_aliases": [],
    }


def _claim_and_complete(client, job_id, final_state, exit_code):
    client.post("/heartbeat", json=_worker_body())
    client.post("/next-job", json=_worker_body())
    r = client.post(
        f"/jobs/{job_id}/complete",
        json={"exit_code": exit_code, "final_state": final_state},
    )
    assert r.status_code == 200, r.text


def _state(client, job_id):
    return client.get(f"/jobs/{job_id}").json()["state"]


def _events(tmp_path):
    p = tmp_path / "logs" / "events.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def test_toctou_child_of_already_failed_parent_is_cancelled_not_stranded(
    client, tmp_path, monkeypatch
):
    parent = _submit(client).json()
    _claim_and_complete(client, parent["id"], "failed", 1)
    assert _state(client, parent["id"]) == "failed"

    # Simulate the race: the point-read reject observed the parent mid-run, so
    # it did not reject. (Empty set only for the app-level reject; the cascade
    # in jobd.broker.state keeps the real _FAILED_SIDE_TERMINAL.)
    monkeypatch.setattr("jobd.app._FAILED_SIDE_TERMINAL", frozenset())
    child = _submit(client, depends_on=[parent["id"]]).json()

    # Without the post-commit sweep this child would be QUEUED forever.
    assert _state(client, child["id"]) == "cancelled"

    cascade = [
        e
        for e in _events(tmp_path)
        if e["event"] == "job_cancelled"
        and e["payload"].get("by") == "cascade"
        and e["job_id"] == child["id"]
    ]
    assert len(cascade) == 1, cascade
    assert cascade[0]["payload"]["parent_job"] == parent["id"]
    assert cascade[0]["payload"]["parent_state"] == "failed"


def test_sweep_no_false_positive_for_running_parent(client):
    """A child of a still-RUNNING parent must stay QUEUED — the sweep only
    cancels children of failed-side-terminal parents."""
    parent = _submit(client).json()
    client.post("/heartbeat", json=_worker_body())
    client.post("/next-job", json=_worker_body())  # parent claimed (assigned)
    assert _state(client, parent["id"]) in ("assigned", "running")  # not terminal

    child = _submit(client, depends_on=[parent["id"]]).json()
    assert _state(client, child["id"]) == "queued"


def test_sweep_no_op_for_completed_parent(client):
    """A child of a COMPLETED parent must stay QUEUED (dispatchable) — COMPLETED
    is not failed-side, so the sweep is a no-op."""
    parent = _submit(client).json()
    _claim_and_complete(client, parent["id"], "completed", 0)
    assert _state(client, parent["id"]) == "completed"

    child = _submit(client, depends_on=[parent["id"]]).json()
    assert _state(client, child["id"]) == "queued"


def test_any_exit_child_of_failed_parent_not_cancelled(client, monkeypatch):
    """depends_on_any_exit children are satisfied by any terminal parent, so the
    sweep must not cancel them even when the parent failed."""
    parent = _submit(client).json()
    _claim_and_complete(client, parent["id"], "failed", 1)

    monkeypatch.setattr("jobd.app._FAILED_SIDE_TERMINAL", frozenset())
    child = _submit(client, depends_on=[parent["id"]], any_exit=True).json()
    assert _state(client, child["id"]) == "queued"
