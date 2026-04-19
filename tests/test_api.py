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
            "project": "orchid-sdxl",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] > 0
    assert body["state"] == "queued"
    assert body["priority"] == 55  # orchid-sdxl default from fixture


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
                "project": "orchid-sdxl",
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
            "project": "orchid-sdxl",
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
            "project": "orchid-sdxl",
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
            "project": "orchid-sdxl",
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
            "project": "orchid-sdxl",
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
            "project": "orchid-sdxl",
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
            "project": "orchid-sdxl",
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
            "host": "gt76",
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
            "host": "gt76",
            "free_vram_gb": 0,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 24,
            "idle_cpus": 12,
        },
    )
    assert r.json()["id"] == job_id


def test_orphan_sweeper_reclaims_after_timeout(client, monkeypatch):
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
            "project": "orchid-sdxl",
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
