"""API tests via FastAPI TestClient (in-process, fast)."""

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


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_submit_minimal(client):
    r = client.post(
        "/submit",
        json={
            "cmd": ["bash", "-c", "echo hi"],
            "cwd": "/tmp",
            "project": "project-x",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] > 0
    assert body["state"] == "queued"
    assert body["priority"] == 55  # project-x default from fixture


def test_submit_with_profile_applies_resources(client):
    r = client.post(
        "/submit",
        json={
            "cmd": ["bash", "train.sh"],
            "cwd": "/tmp",
            "project": "phelipanche",
            "profile": "gpu-heavy",
            "priority_delta": 5,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["priority"] == 85  # 80 + 5
    job_id = body["id"]

    r2 = client.get(f"/jobs/{job_id}")
    assert r2.status_code == 200
    j = r2.json()
    assert j["vram_gb"] == 28
    assert j["ram_gb"] == 22
    assert j["cpus"] == 8


def test_list_jobs(client):
    for i in range(3):
        client.post(
            "/submit",
            json={
                "cmd": ["echo", str(i)],
                "cwd": "/tmp",
                "project": "project-x",
            },
        )
    r = client.get("/jobs")
    assert r.status_code == 200
    assert len(r.json()) == 3


def test_submit_unknown_profile_404(client):
    r = client.post(
        "/submit",
        json={
            "cmd": ["echo", "x"],
            "cwd": "/tmp",
            "project": "project-x",
            "profile": "nonexistent-profile",
        },
    )
    assert r.status_code == 404


def test_classify_unknown_cmd_not_heavy(client):
    r = client.post("/classify", json={"cmd": "ls -la"})
    assert r.status_code == 200
    assert r.json()["heavy"] is False


def test_classify_known_heavy_cmd(client):
    r = client.post("/classify", json={"cmd": "bash train_lora_v5.sh"})
    assert r.status_code == 200
    body = r.json()
    assert body["heavy"] is True
    assert body["rule_id"] == "sdxl-lora-train"
    assert body["suggest_profile"] == "gpu-heavy"


def test_list_workers_empty_before_any_heartbeat(client):
    r = client.get("/workers")
    assert r.status_code == 200
    assert r.json() == []


def test_list_workers_returns_registered_worker(client):
    client.post(
        "/heartbeat",
        json={
            "host": "desktop",
            "host_aliases": ["desktop-worker"],
            "free_vram_gb": 30.0,
            "unregistered_vram_gb": 0.0,
            "free_ram_gb": 28.0,
            "idle_cpus": 10,
            "arch": "x86_64",
            "os": "linux",
            "gpu": True,
            "tags": ["python3", "R", "cuda", "wsl"],
        },
    )
    r = client.get("/workers")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    w = body[0]
    assert w["host"] == "desktop"
    assert w["host_aliases"] == ["desktop-worker"]
    assert w["state"] == "online"
    assert w["gpu"] is True
    assert "R" in w["tags"]
    assert w["last_heartbeat"].endswith("+00:00")


def test_heartbeat_registers_worker(client):
    r = client.post(
        "/heartbeat",
        json={
            "host": "desktop",
            "free_vram_gb": 30.0,
            "unregistered_vram_gb": 0.0,
            "free_ram_gb": 28.0,
            "idle_cpus": 10,
        },
    )
    assert r.status_code == 200


def test_next_job_returns_highest_priority_fitting(client):
    # Submit 2 jobs; desktop worker should get the priority-80 one (phelipanche)
    client.post(
        "/submit",
        json={
            "cmd": ["echo", "1"],
            "cwd": "/tmp",
            "project": "project-x",
            "profile": "small",
            "host_pin": "any",
        },
    )
    client.post(
        "/submit",
        json={
            "cmd": ["echo", "2"],
            "cwd": "/tmp",
            "project": "phelipanche",
            "profile": "small",
            "host_pin": "any",
        },
    )
    # Heartbeat
    client.post(
        "/heartbeat",
        json={
            "host": "desktop",
            "free_vram_gb": 30.0,
            "unregistered_vram_gb": 0.0,
            "free_ram_gb": 28.0,
            "idle_cpus": 10,
        },
    )
    r = client.post(
        "/next-job",
        json={
            "host": "desktop",
            "free_vram_gb": 30.0,
            "unregistered_vram_gb": 0.0,
            "free_ram_gb": 28.0,
            "idle_cpus": 10,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body is not None
    assert body["project"] == "phelipanche"  # priority 80 beats 55
    assert body["state"] == "assigned"
    assert body["worker"] == "desktop"


def test_next_job_empty_queue_returns_null(client):
    client.post(
        "/heartbeat",
        json={
            "host": "desktop",
            "free_vram_gb": 30.0,
            "unregistered_vram_gb": 0.0,
            "free_ram_gb": 28.0,
            "idle_cpus": 10,
        },
    )
    r = client.post(
        "/next-job",
        json={
            "host": "desktop",
            "free_vram_gb": 30.0,
            "unregistered_vram_gb": 0.0,
            "free_ram_gb": 28.0,
            "idle_cpus": 10,
        },
    )
    assert r.status_code == 200
    assert r.json() is None


def test_append_log_and_complete(client):
    sub = client.post(
        "/submit",
        json={
            "cmd": ["echo", "x"],
            "cwd": "/tmp",
            "project": "project-x",
            "profile": "small",
            "host_pin": "any",
        },
    ).json()
    client.post(
        "/heartbeat",
        json={
            "host": "desktop",
            "free_vram_gb": 30.0,
            "unregistered_vram_gb": 0.0,
            "free_ram_gb": 28.0,
            "idle_cpus": 10,
        },
    )
    claim = client.post(
        "/next-job",
        json={
            "host": "desktop",
            "free_vram_gb": 30.0,
            "unregistered_vram_gb": 0.0,
            "free_ram_gb": 28.0,
            "idle_cpus": 10,
        },
    ).json()
    assert claim["id"] == sub["id"]

    r = client.post(f"/jobs/{sub['id']}/log", content=b"hello world\n")
    assert r.status_code == 200
    assert r.json()["bytes"] == len(b"hello world\n")

    r = client.post(
        f"/jobs/{sub['id']}/complete",
        json={"exit_code": 0, "final_state": "completed"},
    )
    assert r.status_code == 200
    assert r.json()["state"] == "completed"

    j = client.get(f"/jobs/{sub['id']}").json()
    assert j["state"] == "completed"
    assert j["exit_code"] == 0


def test_signal_poll(client):
    sub = client.post(
        "/submit",
        json={
            "cmd": ["sleep", "60"],
            "cwd": "/tmp",
            "project": "project-x",
            "profile": "small",
            "host_pin": "any",
        },
    ).json()
    client.post(
        "/heartbeat",
        json={
            "host": "desktop",
            "free_vram_gb": 30.0,
            "unregistered_vram_gb": 0.0,
            "free_ram_gb": 28.0,
            "idle_cpus": 10,
        },
    )
    client.post(
        "/next-job",
        json={
            "host": "desktop",
            "free_vram_gb": 30.0,
            "unregistered_vram_gb": 0.0,
            "free_ram_gb": 28.0,
            "idle_cpus": 10,
        },
    )

    r = client.get(f"/jobs/{sub['id']}/signal")
    assert r.status_code == 200
    assert r.json()["signal"] is None

    client.post(f"/jobs/{sub['id']}/cancel")
    r2 = client.get(f"/jobs/{sub['id']}/signal")
    assert r2.json()["signal"] == "cancel"


def test_list_projects(client):
    r = client.get("/projects")
    assert r.status_code == 200
    assert r.json()["phelipanche"] == 80


def test_set_project_priority(client):
    r = client.post("/projects/phelipanche", json={"priority": 90})
    assert r.status_code == 200
    assert r.json()["phelipanche"] == 90


def test_nudge_project_priority(client):
    r = client.post("/projects/phelipanche/nudge", json={"delta": 5})
    assert r.status_code == 200
    assert r.json()["phelipanche"] == 85


def test_reload_reloads_projects(client, sample_projects_yaml):
    sample_projects_yaml.write_text("""projects:
  phelipanche: { priority: 99 }
  _default: { priority: 40 }
""")
    r = client.post("/reload")
    assert r.status_code == 200
    g = client.get("/projects").json()
    assert g["phelipanche"] == 99


def test_submit_with_requires_persists_and_returns(client):
    r = client.post(
        "/submit",
        json={
            "cmd": ["echo", "hi"],
            "cwd": "/tmp",
            "project": "project-x",
            "requires": {"arch": "x86_64", "gpu": True, "needs": ["cuda"]},
        },
    )
    assert r.status_code == 200
    job_id = r.json()["id"]
    got = client.get(f"/jobs/{job_id}").json()
    assert got["requires"] == {
        "arch": "x86_64",
        "os": "any",
        "gpu": True,
        "needs": ["cuda"],
        "idempotent": False,
    }


def test_heartbeat_persists_capabilities(client):
    r = client.post(
        "/heartbeat",
        json={
            "host": "rpi4",
            "free_vram_gb": 0,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 3.5,
            "idle_cpus": 4,
            "arch": "arm64",
            "os": "linux",
            "gpu": False,
            "tags": ["python3", "always-on"],
            "host_aliases": [],
        },
    )
    assert r.status_code == 200
    r = client.post(
        "/submit",
        json={
            "cmd": ["uname", "-m"],
            "cwd": "/tmp",
            "project": "project-x",
            "requires": {"arch": "arm64"},
        },
    )
    job_id = r.json()["id"]
    r = client.post(
        "/next-job",
        json={
            "host": "rpi4",
            "free_vram_gb": 0,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 3.5,
            "idle_cpus": 4,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body is not None
    assert body["id"] == job_id


def test_next_job_uses_persisted_worker_capabilities(client):
    client.post(
        "/heartbeat",
        json={
            "host": "broker-host",
            "free_vram_gb": 0,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 24,
            "idle_cpus": 12,
            "arch": "x86_64",
            "os": "linux",
            "gpu": False,
            "tags": ["R", "python3"],
            "host_aliases": [],
        },
    )
    r = client.post(
        "/submit",
        json={
            "cmd": ["Rscript", "x.R"],
            "cwd": "/tmp",
            "project": "phelipanche",
            "requires": {"needs": ["R"]},
        },
    )
    job_id = r.json()["id"]
    r = client.post(
        "/next-job",
        json={
            "host": "broker-host",
            "free_vram_gb": 0,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 24,
            "idle_cpus": 12,
        },
    )
    assert r.json()["id"] == job_id


def test_orphan_sweeper_reclaims_after_timeout(client):
    """A job assigned to a worker whose heartbeat went silent >5min must be re-queued."""
    from datetime import UTC, datetime, timedelta

    from jobd import app as app_mod
    from sqlalchemy import update

    # Worker heartbeats once
    client.post(
        "/heartbeat",
        json={
            "host": "ghost",
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
    # Submit + claim
    r = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-x",
        },
    )
    job_id = r.json()["id"]
    claim = client.post(
        "/next-job",
        json={
            "host": "ghost",
            "free_vram_gb": 0,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 8,
            "idle_cpus": 4,
        },
    )
    assert claim.json()["id"] == job_id
    assert claim.json()["state"] == "assigned"

    # Fast-forward: manually backdate the worker's heartbeat >5min
    from jobd.db import Worker

    engine = app_mod._engine_for_testing()
    with engine.begin() as conn:
        conn.execute(
            update(Worker)
            .where(Worker.host == "ghost")
            .values(last_heartbeat=datetime.now(UTC) - timedelta(minutes=6))
        )

    # Trigger sweeper manually (exposed via private test hook)
    app_mod._sweep_once()

    got = client.get(f"/jobs/{job_id}").json()
    assert got["state"] == "queued"
    assert got["worker"] is None


def test_unmatcheable_job_gets_warning(client):
    """Job requiring arm64 with no arm64 worker advertising gets a warning."""
    from datetime import UTC, datetime, timedelta

    from jobd import app as app_mod
    from jobd.db import Job
    from sqlalchemy import update

    client.post(
        "/heartbeat",
        json={
            "host": "desktop",
            "free_vram_gb": 10,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 8,
            "idle_cpus": 4,
            "arch": "x86_64",
            "os": "linux",
            "gpu": True,
            "tags": [],
            "host_aliases": [],
        },
    )
    r = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-x",
            "requires": {"arch": "arm64"},
        },
    )
    job_id = r.json()["id"]

    got = client.get(f"/jobs/{job_id}").json()
    assert got["warning"] is None

    engine = app_mod._engine_for_testing()
    with engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(submitted_at=datetime.now(UTC) - timedelta(seconds=90))
        )

    app_mod._sweep_once()
    got = client.get(f"/jobs/{job_id}").json()
    assert got["warning"] is not None
    assert "no matching worker" in got["warning"].lower()


def test_no_requires_job_never_gets_warning_on_empty_fleet(client):
    """A job with no requires block is matcheable by definition — empty fleet != mismatch."""
    from datetime import UTC, datetime, timedelta

    from jobd import app as app_mod
    from jobd.db import Job
    from sqlalchemy import update

    # No heartbeats — empty fleet
    r = client.post(
        "/submit",
        json={"cmd": ["true"], "cwd": "/tmp", "project": "project-x"},
    )
    job_id = r.json()["id"]

    engine = app_mod._engine_for_testing()
    with engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(submitted_at=datetime.now(UTC) - timedelta(seconds=90))
        )

    app_mod._sweep_once()
    assert client.get(f"/jobs/{job_id}").json()["warning"] is None


def test_warning_clears_when_matching_worker_appears(client):
    """Warning set on unmatcheable job clears once a capable worker heartbeats."""
    from datetime import UTC, datetime, timedelta

    from jobd import app as app_mod
    from jobd.db import Job
    from sqlalchemy import update

    client.post(
        "/heartbeat",
        json={
            "host": "x86box",
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
    r = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-x",
            "requires": {"arch": "arm64"},
        },
    )
    job_id = r.json()["id"]

    engine = app_mod._engine_for_testing()
    with engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(submitted_at=datetime.now(UTC) - timedelta(seconds=90))
        )
    app_mod._sweep_once()
    assert client.get(f"/jobs/{job_id}").json()["warning"] is not None

    # arm64 worker joins the fleet
    client.post(
        "/heartbeat",
        json={
            "host": "pi4",
            "free_vram_gb": 0,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 3,
            "idle_cpus": 4,
            "arch": "arm64",
            "os": "linux",
            "gpu": False,
            "tags": [],
            "host_aliases": [],
        },
    )
    app_mod._sweep_once()
    assert client.get(f"/jobs/{job_id}").json()["warning"] is None


def test_orphan_sweeper_idempotent_reclaims_at_90s(client):
    """A job with requires.idempotent=true reclaims after 90s, not 5min."""
    from datetime import UTC, datetime, timedelta

    from jobd import app as app_mod
    from jobd.db import Worker
    from sqlalchemy import update

    client.post(
        "/heartbeat",
        json={
            "host": "ghost-idem",
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
    r = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-x",
            "requires": {"idempotent": True},
        },
    )
    job_id = r.json()["id"]
    claim = client.post(
        "/next-job",
        json={
            "host": "ghost-idem",
            "free_vram_gb": 0,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 8,
            "idle_cpus": 4,
        },
    )
    assert claim.json()["id"] == job_id

    engine = app_mod._engine_for_testing()
    # 2 minutes silent: past idempotent cutoff (90s) but under the 5min default
    with engine.begin() as conn:
        conn.execute(
            update(Worker)
            .where(Worker.host == "ghost-idem")
            .values(last_heartbeat=datetime.now(UTC) - timedelta(seconds=120))
        )

    app_mod._sweep_once()

    got = client.get(f"/jobs/{job_id}").json()
    assert got["state"] == "queued"
    assert got["worker"] is None


# ---------- depends_on job chaining ----------


def _submit(client, project="project-x", cmd=None, depends_on=None, any_exit=False):
    body = {
        "cmd": cmd or ["true"],
        "cwd": "/tmp",
        "project": project,
    }
    if depends_on is not None:
        body["depends_on"] = depends_on
    if any_exit:
        body["depends_on_any_exit"] = True
    r = client.post("/submit", json=body)
    return r


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


def test_submit_unknown_parent_rejected(client):
    r = _submit(client, depends_on=[9999])
    assert r.status_code == 400
    assert "parent" in r.text.lower() or "depends_on" in r.text.lower()


def test_child_not_dispatched_while_parent_queued(client):
    parent = _submit(client).json()
    child = _submit(client, depends_on=[parent["id"]]).json()
    _heartbeat(client)
    got = _next_job(client).json()
    assert got["id"] == parent["id"]
    got2 = _next_job(client).json()
    assert got2 is None or got2 == {}
    row = client.get(f"/jobs/{child['id']}").json()
    assert row["state"] == "queued"


def test_child_dispatchable_after_parent_completes(client):
    parent = _submit(client).json()
    child = _submit(client, depends_on=[parent["id"]]).json()
    _heartbeat(client)
    _next_job(client)
    client.post(f"/jobs/{parent['id']}/complete", json={"exit_code": 0})
    claim = _next_job(client).json()
    assert claim["id"] == child["id"]


def test_parent_failure_cancels_child(client):
    parent = _submit(client).json()
    child = _submit(client, depends_on=[parent["id"]]).json()
    _heartbeat(client)
    _next_job(client)
    client.post(
        f"/jobs/{parent['id']}/complete",
        json={"exit_code": 1, "final_state": "failed"},
    )
    row = client.get(f"/jobs/{child['id']}").json()
    assert row["state"] == "cancelled"
    assert row.get("warning")


def test_parent_cancelled_queued_cancels_child(client):
    parent = _submit(client).json()
    child = _submit(client, depends_on=[parent["id"]]).json()
    client.post(f"/jobs/{parent['id']}/cancel")
    row = client.get(f"/jobs/{child['id']}").json()
    assert row["state"] == "cancelled"


def test_depends_on_any_exit_allows_failed_parent(client):
    parent = _submit(client).json()
    child = _submit(client, depends_on=[parent["id"]], any_exit=True).json()
    _heartbeat(client)
    _next_job(client)
    client.post(
        f"/jobs/{parent['id']}/complete",
        json={"exit_code": 2, "final_state": "failed"},
    )
    row = client.get(f"/jobs/{child['id']}").json()
    assert row["state"] == "queued"
    claim = _next_job(client).json()
    assert claim["id"] == child["id"]


def test_output_endpoint_returns_tail(client):
    """Streaming logs via /log should be retrievable via /output."""
    job = _submit(client).json()
    # Simulate worker streaming chunks
    client.post(f"/jobs/{job['id']}/log", content=b"line 1\n")
    client.post(f"/jobs/{job['id']}/log", content=b"line 2\n")
    client.post(f"/jobs/{job['id']}/log", content=b"line 3\n")
    r = client.get(f"/jobs/{job['id']}/output")
    assert r.status_code == 200
    body = r.json()
    assert body["tail"] == "line 1\nline 2\nline 3\n"
    assert body["size_bytes"] == 21
    assert body["truncated"] is False


def test_output_endpoint_truncates_to_tail_window(client):
    """tail=5 on a 21-byte file returns the last 5 bytes + truncated=True."""
    job = _submit(client).json()
    client.post(f"/jobs/{job['id']}/log", content=b"line 1\nline 2\nline 3\n")
    r = client.get(f"/jobs/{job['id']}/output", params={"tail": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["tail"] == "ine 3\n"[-5:]
    assert body["size_bytes"] == 21
    assert body["returned_bytes"] == 5
    assert body["truncated"] is True


def test_output_endpoint_empty_when_no_log(client):
    """Job exists but worker never streamed → size_bytes=0, empty tail."""
    job = _submit(client).json()
    r = client.get(f"/jobs/{job['id']}/output")
    assert r.status_code == 200
    body = r.json()
    assert body["size_bytes"] == 0
    assert body["tail"] == ""


def test_output_endpoint_404_on_unknown_job(client):
    r = client.get("/jobs/99999/output")
    assert r.status_code == 404


def test_complete_auto_derives_failed_from_nonzero_rc(client):
    """If the worker omits final_state, nonzero rc → failed, zero → completed.
    Matters for the depends_on cascade, which keys off terminal state."""
    job_ok = _submit(client).json()
    job_bad = _submit(client).json()
    _heartbeat(client)
    _next_job(client)
    _next_job(client)
    # worker omits final_state
    client.post(f"/jobs/{job_ok['id']}/complete", json={"exit_code": 0})
    client.post(f"/jobs/{job_bad['id']}/complete", json={"exit_code": 1})
    assert client.get(f"/jobs/{job_ok['id']}").json()["state"] == "completed"
    assert client.get(f"/jobs/{job_bad['id']}").json()["state"] == "failed"


def test_complete_clears_pending_signal(client):
    """After worker posts /complete, job.signal must be None so a future
    reclaim/retry doesn't see a stale cancel signal."""
    job = _submit(client).json()
    _heartbeat(client)
    _next_job(client)
    client.post(f"/jobs/{job['id']}/cancel")
    assert client.get(f"/jobs/{job['id']}/signal").json()["signal"] == "cancel"
    client.post(
        f"/jobs/{job['id']}/complete",
        json={"exit_code": -15, "final_state": "cancelled"},
    )
    assert client.get(f"/jobs/{job['id']}/signal").json()["signal"] is None


def test_job_info_exposes_session_id(client):
    r = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-x",
            "session_id": "sess-abc-123",
        },
    )
    assert r.status_code == 200
    assert r.json()["session_id"] == "sess-abc-123"
    got = client.get(f"/jobs/{r.json()['id']}").json()
    assert got["session_id"] == "sess-abc-123"


def test_submit_rejects_mnt_c_cwd_for_non_laptop(client):
    """Windows-mount paths routed to a non-laptop host → 400, not queued."""
    r = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "~/some/project",
            "project": "project-x",
            "host_pin": "desktop-worker",
        },
    )
    assert r.status_code == 400
    assert "/mnt/c/" in r.text


def test_submit_allows_mnt_c_cwd_when_pinned_to_laptop(client):
    r = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "~/some/project",
            "project": "project-x",
            "host_pin": "laptop",
        },
    )
    assert r.status_code == 200


def test_cancel_running_sets_signal_not_state(client):
    """POST /cancel on a claimed job must set job.signal='cancel' and
    leave state=assigned — the worker completes the transition after
    SIGTERMing the child."""
    job = _submit(client).json()
    _heartbeat(client)
    claim = _next_job(client).json()
    assert claim["id"] == job["id"]

    r = client.post(f"/jobs/{job['id']}/cancel")
    assert r.status_code == 200
    row = r.json()
    # state unchanged — worker must finish the job before it flips
    assert row["state"] in ("assigned", "running")

    sig = client.get(f"/jobs/{job['id']}/signal").json()
    assert sig["signal"] == "cancel"

    # worker reports the terminal state once the child has exited
    client.post(
        f"/jobs/{job['id']}/complete",
        json={"exit_code": -15, "final_state": "cancelled"},
    )
    row = client.get(f"/jobs/{job['id']}").json()
    assert row["state"] == "cancelled"
    assert row["exit_code"] == -15


def test_submit_fast_path_profile_stamps_flag(client):
    """Profiles declaring fast_path: true must propagate to the job so the
    worker can skip heavy-run wrapping. Without this, the flag lives only
    in profiles.yaml and nobody reads it."""
    r = client.post(
        "/submit",
        json={
            "cmd": ["echo", "hi"],
            "cwd": "/tmp",
            "project": "project-x",
            "profile": "small",
        },
    )
    assert r.status_code == 200
    assert r.json()["fast_path"] is True


def test_submit_non_fast_path_profile_leaves_flag_false(client):
    r = client.post(
        "/submit",
        json={
            "cmd": ["bash", "train.sh"],
            "cwd": "/tmp",
            "project": "project-x",
            "profile": "gpu-heavy",
        },
    )
    assert r.status_code == 200
    assert r.json()["fast_path"] is False


def test_submit_no_profile_defaults_fast_path_false(client):
    r = client.post(
        "/submit",
        json={"cmd": ["true"], "cwd": "/tmp", "project": "project-x"},
    )
    assert r.json()["fast_path"] is False


def test_heartbeat_persists_mount_roots_and_workers_returns_them(client):
    client.post(
        "/heartbeat",
        json={
            "host": "desktop-worker",
            "free_vram_gb": 20,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 30,
            "idle_cpus": 8,
            "arch": "x86_64",
            "os": "linux",
            "gpu": True,
            "tags": [],
            "host_aliases": [],
            "mount_roots": ["/home", "/mnt/d", "/tmp"],
        },
    )
    r = client.get("/workers")
    assert r.status_code == 200
    rows = [w for w in r.json() if w["host"] == "desktop-worker"]
    assert rows and rows[0]["mount_roots"] == ["/home", "/mnt/d", "/tmp"]


def test_next_job_skips_job_with_cwd_outside_worker_mount_roots(client):
    """Worker that advertises mount_roots must not receive jobs whose cwd
    isn't under any of them — otherwise the job fails at subprocess spawn
    with cwd-does-not-exist."""
    client.post(
        "/heartbeat",
        json={
            "host": "desktop-worker",
            "free_vram_gb": 20,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 30,
            "idle_cpus": 8,
            "host_aliases": [],
            "mount_roots": ["/home", "/tmp"],
        },
    )
    r = client.post(
        "/submit",
        json={
            "cmd": ["ls"],
            "cwd": "/mnt/d/some/project",
            "project": "project-x",
            "host_pin": "any",
        },
    )
    assert r.status_code == 200
    got = client.post(
        "/next-job",
        json={
            "host": "desktop-worker",
            "free_vram_gb": 20,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 30,
            "idle_cpus": 8,
        },
    ).json()
    assert got is None or got == {}


def test_next_job_empty_mount_roots_disables_filter(client):
    """A worker that doesn't advertise mount_roots (older binary) gets the
    old behavior: no filtering. Backwards-compat guard — don't starve the
    pre-2026-04-24 fleet."""
    client.post(
        "/heartbeat",
        json={
            "host": "legacy-worker",
            "free_vram_gb": 0,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 8,
            "idle_cpus": 4,
            "host_aliases": [],
        },
    )
    r = client.post(
        "/submit",
        json={
            "cmd": ["ls"],
            "cwd": "/some/unusual/path",
            "project": "project-x",
        },
    )
    job_id = r.json()["id"]
    got = client.post(
        "/next-job",
        json={
            "host": "legacy-worker",
            "free_vram_gb": 0,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 8,
            "idle_cpus": 4,
        },
    ).json()
    assert got is not None and got["id"] == job_id


def test_next_job_mixed_fleet_routes_by_mount_root(client):
    """Two workers with disjoint roots; each only claims jobs under its own root."""
    for host, roots in (("host-a", ["/home"]), ("host-b", ["/mnt/d"])):
        client.post(
            "/heartbeat",
            json={
                "host": host,
                "free_vram_gb": 0,
                "unregistered_vram_gb": 0,
                "free_ram_gb": 8,
                "idle_cpus": 4,
                "host_aliases": [],
                "mount_roots": roots,
            },
        )
    job_home = client.post(
        "/submit",
        json={"cmd": ["ls"], "cwd": "~/proj", "project": "project-x"},
    ).json()
    job_d = client.post(
        "/submit",
        json={"cmd": ["ls"], "cwd": "/mnt/d/data", "project": "project-x"},
    ).json()

    def _poll(host):
        return client.post(
            "/next-job",
            json={
                "host": host,
                "free_vram_gb": 0,
                "unregistered_vram_gb": 0,
                "free_ram_gb": 8,
                "idle_cpus": 4,
            },
        ).json()

    got_a = _poll("host-a")
    assert got_a is not None and got_a["id"] == job_home["id"]
    got_b = _poll("host-b")
    assert got_b is not None and got_b["id"] == job_d["id"]


def test_fanin_requires_all_parents_complete(client):
    p1 = _submit(client).json()
    p2 = _submit(client).json()
    child = _submit(client, depends_on=[p1["id"], p2["id"]]).json()
    _heartbeat(client)
    first = _next_job(client).json()
    client.post(f"/jobs/{first['id']}/complete", json={"exit_code": 0})
    second = _next_job(client).json()
    assert second["id"] in (p1["id"], p2["id"])
    assert second["id"] != child["id"]
    client.post(f"/jobs/{second['id']}/complete", json={"exit_code": 0})
    third = _next_job(client).json()
    assert third["id"] == child["id"]
