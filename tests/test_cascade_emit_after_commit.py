"""Cascade cancellation emits job_cancelled events AFTER commit.

_cascade_on_parent_terminal used to emit inside the function, before the
caller's session.commit() — so a failed commit could leave events.jsonl
claiming a cancel the DB never recorded. The fix moves emission to each call
site, after commit. This test pins the observable contract: exactly one
job_cancelled event per cascade-cancelled child, with by='cascade'.
"""

import json


def _read_events(tmp_path):
    p = tmp_path / "logs" / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _submit(client, depends_on=None):
    body = {"cmd": ["true"], "cwd": "/tmp", "project": "project-a"}
    if depends_on is not None:
        body["depends_on"] = depends_on
    return client.post("/submit", json=body)


def _heartbeat(client, host="w1"):
    client.post(
        "/heartbeat",
        json={
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
        },
    )


def _next_job(client, host="w1"):
    return client.post(
        "/next-job",
        json={
            "host": host,
            "free_vram_gb": 0,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 8,
            "idle_cpus": 4,
        },
    )


def test_parent_failure_emits_one_cascade_event_per_child(client, tmp_path):
    parent = _submit(client).json()
    child_a = _submit(client, depends_on=[parent["id"]]).json()
    child_b = _submit(client, depends_on=[parent["id"]]).json()
    _heartbeat(client)
    _next_job(client)
    client.post(
        f"/jobs/{parent['id']}/complete",
        json={"exit_code": 1, "final_state": "failed"},
    )

    events = _read_events(tmp_path)
    cascade = [
        e for e in events if e["event"] == "job_cancelled" and e["payload"].get("by") == "cascade"
    ]
    by_child = {e["job_id"]: e for e in cascade}
    assert set(by_child) == {child_a["id"], child_b["id"]}, cascade
    # exactly one per child (no dup from a leftover in-function emit)
    assert len(cascade) == 2, cascade
    for cid in (child_a["id"], child_b["id"]):
        e = by_child[cid]
        assert e["project"] == "project-a"
        assert e["payload"]["parent_job"] == parent["id"]
        assert e["payload"]["parent_state"] == "failed"
        assert e["payload"]["prior_state"] == "queued"

    # The cascade emit must land AFTER the parent's job_completed (i.e. after
    # the caller's commit), not before it.
    order = [e["event"] for e in events]
    completed_ix = order.index("job_completed")
    for e in cascade:
        assert events.index(e) > completed_ix, order
