"""End-to-end tests — require a running broker + worker.

Run manually: `JOBD_E2E=1 pytest tests/test_e2e.py -v`
"""
import os
import time

import httpx
import pytest

BASE = os.environ.get("JOBD_URL", "http://127.0.0.1:8765")
pytestmark = pytest.mark.skipif(
    os.environ.get("JOBD_E2E") != "1",
    reason="Set JOBD_E2E=1 to run",
)


@pytest.fixture
def client():
    with httpx.Client(base_url=BASE, timeout=60.0) as c:
        yield c


def test_worker_is_alive(client):
    r = client.get("/health")
    assert r.status_code == 200
    sub = client.post(
        "/submit",
        json={"cmd": ["echo", "alive"], "cwd": "/tmp", "project": "project-a",
              "profile": "cpu-quick", "host_pin": "any"},
    )
    job_id = sub.json()["id"]
    deadline = time.time() + 30
    last = None
    while time.time() < deadline:
        last = client.get(f"/jobs/{job_id}").json()
        if last["state"] in ("completed", "failed"):
            assert last["state"] == "completed", f"job failed: {last}"
            assert last["exit_code"] == 0
            return
        time.sleep(1)
    pytest.fail(f"worker did not complete job in 30s; last state: {last}")


def test_five_real_jobs(client):
    """Success criterion for Phase 1: 5 jobs submitted + completed end-to-end."""
    job_ids = []
    for i in range(5):
        sub = client.post(
            "/submit",
            json={"cmd": ["bash", "-c", f"echo job {i}; sleep 1; echo done {i}"],
                  "cwd": "/tmp", "project": "project-a",
                  "profile": "cpu-quick", "host_pin": "any"},
        )
        job_ids.append(sub.json()["id"])

    deadline = time.time() + 120
    while time.time() < deadline:
        states = [client.get(f"/jobs/{i}").json()["state"] for i in job_ids]
        if all(s in ("completed", "failed") for s in states):
            mapping = dict(zip(job_ids, states))
            assert all(s == "completed" for s in states), f"mixed states: {mapping}"
            return
        time.sleep(2)
    final = [client.get(f"/jobs/{i}").json()["state"] for i in job_ids]
    pytest.fail(f"timed out: {final}")
