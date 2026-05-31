"""Phase 5 acceptance: BACKLOG criterion answerable in one job audit call."""

from __future__ import annotations


def _heartbeat_payload(host="laptop"):
    return {
        "host": host,
        "host_aliases": [host, "any"],
        "free_vram_gb": 0,
        "unregistered_vram_gb": 0,
        "free_ram_gb": 32,
        "idle_cpus": 8,
        "arch": "x86_64",
        "os": "linux",
        "gpu": False,
        "tags": [],
        "mount_roots": ["/tmp", "/home"],
    }


def test_acceptance_project_p_seven_days(client, logs_dir):
    """BACKLOG: 'for project P over last 7 days, list every job, final state,
    wall time, and matcher-refusal warnings' — answerable from GET /events alone."""
    project = "project-b"

    # 1. Submit 3 jobs in project P
    job_ids: list[int] = []
    for _ in range(3):
        r = client.post(
            "/submit",
            json={"project": project, "cmd": ["true"], "cwd": "/tmp", "host_pin": "any"},
        )
        assert r.status_code == 200, r.text
        job_ids.append(r.json()["id"])

    # 2. Dispatch + start + complete the first two via worker handshake
    client.post("/heartbeat", json=_heartbeat_payload())
    for jid in job_ids[:2]:
        r = client.post("/next-job", json=_heartbeat_payload())
        assert r.status_code == 200
        body = r.json()
        assert body is not None and body["id"] == jid, f"expected to dispatch {jid}, got {body}"
        r1 = client.post(f"/jobs/{jid}/started")
        assert r1.status_code == 200, r1.text
        r2 = client.post(
            f"/jobs/{jid}/complete",
            json={"exit_code": 0, "final_state": "completed"},
        )
        assert r2.status_code == 200, r2.text

    # 3. Cancel the third while queued
    r = client.post(f"/jobs/{job_ids[2]}/cancel")
    assert r.status_code == 200, r.text

    # 4. Sub-question 4: trigger a submit_warning by submitting under a novel project
    r = client.post(
        "/submit",
        json={"project": "phaseV-novel-2026", "cmd": ["true"], "cwd": "/tmp", "host_pin": "any"},
    )
    assert r.status_code == 200, r.text
    novel_id = r.json()["id"]

    # 5. Single audit query as `job audit --project project-b --since 7d` would issue
    r = client.get("/events", params={"project": project, "since": "7d"})
    assert r.status_code == 200
    rows = r.json()

    by_id: dict[int, list[dict]] = {}
    for row in rows:
        if row.get("job_id") is not None:
            by_id.setdefault(row["job_id"], []).append(row)

    # Sub-question 1: every project-P job appears
    assert set(by_id.keys()) == set(job_ids), (
        f"missing jobs in audit output: expected {job_ids}, got {sorted(by_id.keys())}"
    )

    # Sub-question 2: final state derivable per job
    terminal = {"job_completed", "job_cancelled", "job_failed"}
    for jid in job_ids:
        events = [e["event"] for e in by_id[jid]]
        assert any(e in terminal for e in events), f"job {jid} has no terminal event: {events}"

    # Sub-question 3: wall_s present on completed-job rows
    for jid in job_ids[:2]:
        completed = [e for e in by_id[jid] if e["event"] == "job_completed"]
        assert completed, f"job {jid} missing job_completed"
        assert completed[-1]["payload"].get("wall_s") is not None, (
            f"job {jid} job_completed payload missing wall_s"
        )

    # Sub-question 4: matcher-refusal warnings derivable from a parallel query
    r = client.get("/events", params={"project": "phaseV-novel-2026"})
    assert r.status_code == 200
    novel_rows = r.json()
    warnings = [e for e in novel_rows if e["event"] == "submit_warning" and e["job_id"] == novel_id]
    assert warnings, (
        f"submit_warning event missing for unknown project; "
        f"got events={[e['event'] for e in novel_rows]}"
    )
    assert "phaseV-novel-2026" in warnings[0]["payload"]["warning_text"]
