"""Repro: the REAL H-1 race — parent terminalizes in another session BETWEEN
the submit validation read (app.py:336) and the post-commit re-check
(app.py:513). Uses the actual /submit and /complete code paths; the concurrent
terminalization is done via a second SQLAlchemy session (equivalent to the
worker's /complete request landing mid-submit), injected by patching
_serialization_warning which runs after the validation read and before insert.
"""

from fastapi.testclient import TestClient

import jobd.broker.submit as submit_mod
from jobd.app import build_app
from jobd.broker.state import _cascade_on_parent_terminal
from jobd.db import Job
from jobd.models import JobState


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


def test_h1_real_race(tmp_path, sample_projects_yaml, sample_profiles_yaml, sample_classifier_yaml):
    app = build_app(
        db_url=f"sqlite:///{tmp_path}/jobd.db",
        projects_path=sample_projects_yaml,
        profiles_path=sample_profiles_yaml,
        classifier_path=sample_classifier_yaml,
        logs_path=tmp_path / "logs",
    )
    client = TestClient(app)
    SessionLocal = app.state.SessionLocal

    # Parent submitted and claimed -> RUNNING-ish (assigned). Point-read reject
    # will see a non-terminal state, as in the real race.
    parent = client.post(
        "/submit", json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"}
    ).json()
    client.post("/heartbeat", json=_worker_body())
    client.post("/next-job", json=_worker_body())
    pid = parent["id"]
    st = client.get(f"/jobs/{pid}").json()["state"]
    assert st in ("assigned", "running"), st

    # Inject the concurrent terminalization mid-submit: after the validation
    # loop has read parent.state (non-terminal), before the child insert.
    # Patch in jobd.broker.submit, NOT jobd.app: that is the module whose globals
    # submit_job resolves _serialization_warning from. Patching jobd.app here would
    # be a SILENT NO-OP — the race would never be injected, this test would pass, and
    # it would be proving nothing. See jobd/broker/submit.py's docstring.
    orig = submit_mod._serialization_warning
    fired = {"done": False}

    def race_hook(*a, **kw):
        if not fired["done"]:
            fired["done"] = True
            with SessionLocal() as s2:  # the "other request's" session
                p = s2.get(Job, pid)
                p.state = JobState.FAILED.value
                p.exit_code = 1
                s2.commit()
                # the /complete path would also run the cascade over the
                # then-current QUEUED set (child not inserted yet) — a no-op:
                _cascade_on_parent_terminal(s2, p)
                s2.commit()
        return orig(*a, **kw)

    submit_mod._serialization_warning = race_hook
    try:
        child = client.post(
            "/submit",
            json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a", "depends_on": [pid]},
        ).json()
    finally:
        submit_mod._serialization_warning = orig

    assert client.get(f"/jobs/{pid}").json()["state"] == "failed"
    child_state = client.get(f"/jobs/{child['id']}").json()["state"]
    # If H-1 is truly closed the child must be cancelled. QUEUED = strand = fix no-op.
    assert child_state == "cancelled", (
        f"H-1 NOT closed: child is {child_state!r} (stranded) — stale identity-map parent read"
    )
