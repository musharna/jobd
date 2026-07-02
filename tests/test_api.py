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
            "project": "project-a",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] > 0
    assert body["state"] == "queued"
    assert body["priority"] == 55  # project-a default from fixture


def test_submit_persists_submitted_via_cli(client):
    """#51: submitted_via='cli' on the request body must round-trip through
    the broker DB so observability queries can count CLI vs MCP traffic."""
    r = client.post(
        "/submit",
        json={
            "cmd": ["bash", "-c", "x"],
            "cwd": "/tmp",
            "project": "project-a",
            "submitted_via": "cli",
        },
    )
    assert r.status_code == 200
    job_id = r.json()["id"]
    r2 = client.get(f"/jobs/{job_id}")
    assert r2.json()["submitted_via"] == "cli"


def test_submit_persists_submitted_via_mcp(client):
    """#51 mirror: submitted_via='mcp' round-trips identically."""
    r = client.post(
        "/submit",
        json={
            "cmd": ["bash", "-c", "x"],
            "cwd": "/tmp",
            "project": "project-a",
            "submitted_via": "mcp",
        },
    )
    assert r.status_code == 200
    job_id = r.json()["id"]
    r2 = client.get(f"/jobs/{job_id}")
    assert r2.json()["submitted_via"] == "mcp"


def test_submit_rejects_unknown_submitted_via(client):
    """submitted_via is constrained to the cli|mcp Literal — strings outside
    the set are rejected at the Pydantic boundary."""
    r = client.post(
        "/submit",
        json={
            "cmd": ["bash", "-c", "x"],
            "cwd": "/tmp",
            "project": "project-a",
            "submitted_via": "garbage",
        },
    )
    assert r.status_code == 422


def test_submit_omits_submitted_via_persists_null(client):
    """Old clients that don't pass submitted_via must still work; the column
    stays NULL so historical queries can distinguish 'pre-#51' from cli/mcp."""
    r = client.post(
        "/submit",
        json={"cmd": ["bash", "-c", "x"], "cwd": "/tmp", "project": "project-a"},
    )
    assert r.status_code == 200
    job_id = r.json()["id"]
    r2 = client.get(f"/jobs/{job_id}")
    assert r2.json()["submitted_via"] is None


def test_submit_with_profile_applies_resources(client):
    r = client.post(
        "/submit",
        json={
            "cmd": ["bash", "train.sh"],
            "cwd": "/tmp",
            "project": "project-b",
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
                "project": "project-a",
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
            "project": "project-a",
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
            "host_aliases": ["desktop-vm"],
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
    assert w["host_aliases"] == ["desktop-vm"]
    assert w["state"] == "online"
    assert w["gpu"] is True
    assert "R" in w["tags"]
    assert w["last_heartbeat"].endswith("+00:00")
    # A heartbeat that omits the slot fields reads as single-slot, idle.
    assert w["max_concurrent"] == 1
    assert w["running"] == 0


def test_list_workers_surfaces_slot_usage(client):
    client.post(
        "/heartbeat",
        json={
            "host": "desktop",
            "free_vram_gb": 9.1,
            "unregistered_vram_gb": 0.0,
            "free_ram_gb": 28.0,
            "idle_cpus": 6,
            "gpu": True,
            "max_concurrent": 3,
            "running": 2,
        },
    )
    w = client.get("/workers").json()[0]
    assert w["max_concurrent"] == 3
    assert w["running"] == 2


def test_delete_worker_404_when_unknown(client):
    r = client.delete("/workers/nosuchhost")
    assert r.status_code == 404


def test_delete_worker_409_when_online(client):
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
    r = client.delete("/workers/desktop")
    assert r.status_code == 409
    assert "online" in r.json()["detail"].lower()
    # Row still present
    listing = client.get("/workers").json()
    assert any(w["host"] == "desktop" for w in listing)


def test_delete_offline_worker_succeeds(client):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from jobd import app as app_mod
    from jobd.db import Worker

    client.post(
        "/heartbeat",
        json={
            "host": "ghosthost",
            "free_vram_gb": 0.0,
            "unregistered_vram_gb": 0.0,
            "free_ram_gb": 4.0,
            "idle_cpus": 2,
        },
    )
    engine = app_mod._engine_for_testing()
    with engine.begin() as conn:
        conn.execute(
            update(Worker)
            .where(Worker.host == "ghosthost")
            .values(state="offline", last_heartbeat=datetime.now(UTC) - timedelta(minutes=10))
        )
    r = client.delete("/workers/ghosthost")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["deleted"] == "ghosthost"
    listing = client.get("/workers").json()
    assert all(w["host"] != "ghosthost" for w in listing)


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
    # Submit 2 jobs; desktop worker should get the priority-80 one (project-b)
    client.post(
        "/submit",
        json={
            "cmd": ["echo", "1"],
            "cwd": "/tmp",
            "project": "project-a",
            "profile": "small",
            "host_pin": "any",
        },
    )
    client.post(
        "/submit",
        json={
            "cmd": ["echo", "2"],
            "cwd": "/tmp",
            "project": "project-b",
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
    assert body["project"] == "project-b"  # priority 80 beats 55
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


def test_next_job_longpoll_times_out_returns_null(client):
    """M4: an async long-poll on an empty queue must return None near wait_s
    without holding a threadpool thread. Here it should return in ~0.4s, not
    hang and not return early."""
    import time as _time

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
    t0 = _time.monotonic()
    r = client.post(
        "/next-job",
        json={
            "host": "desktop",
            "free_vram_gb": 30.0,
            "unregistered_vram_gb": 0.0,
            "free_ram_gb": 28.0,
            "idle_cpus": 10,
            "wait_s": 0.4,
        },
    )
    elapsed = _time.monotonic() - t0
    assert r.status_code == 200
    assert r.json() is None
    assert 0.3 < elapsed < 3.0, f"long-poll should wait ~wait_s, took {elapsed:.2f}s"


def test_next_job_longpoll_wakes_on_submit(client):
    """M4: a submit that lands while a worker is long-polling must WAKE the poll
    (via the cross-thread asyncio.Event), returning the job well before the
    wait_s deadline and before the _LONGPOLL_RECHECK_S backstop — proving the
    wait suspends on the loop rather than parking a thread until recheck."""
    import threading
    import time as _time

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
    result: dict = {}

    def _poll():
        t0 = _time.monotonic()
        r = client.post(
            "/next-job",
            json={
                "host": "desktop",
                "free_vram_gb": 30.0,
                "unregistered_vram_gb": 0.0,
                "free_ram_gb": 28.0,
                "idle_cpus": 10,
                "wait_s": 8.0,
            },
        )
        result["elapsed"] = _time.monotonic() - t0
        result["body"] = r.json()

    poller = threading.Thread(target=_poll)
    poller.start()
    _time.sleep(0.4)  # let the poll start and suspend on the wake event
    client.post("/submit", json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"})
    poller.join(timeout=5.0)

    assert not poller.is_alive(), "long-poll did not return after submit"
    assert result["body"] is not None and result["body"]["state"] == "assigned"
    # Woken by the submit, not by the 10s recheck backstop.
    assert result["elapsed"] < 5.0, f"wake was slow ({result['elapsed']:.2f}s) — recheck, not wake?"


def test_append_log_and_complete(client):
    sub = client.post(
        "/submit",
        json={
            "cmd": ["echo", "x"],
            "cwd": "/tmp",
            "project": "project-a",
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
            "project": "project-a",
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
    assert r.json()["project-b"]["priority"] == 80


def test_set_project_priority(client):
    r = client.post("/projects/project-b", json={"priority": 90})
    assert r.status_code == 200
    assert r.json()["project-b"]["priority"] == 90


def test_nudge_project_priority(client):
    r = client.post("/projects/project-b/nudge", json={"delta": 5})
    assert r.status_code == 200
    assert r.json()["project-b"]["priority"] == 85


def test_reload_reloads_projects(client, sample_projects_yaml):
    sample_projects_yaml.write_text("""projects:
  project-b: { priority: 99 }
  _default: { priority: 40 }
""")
    r = client.post("/reload")
    assert r.status_code == 200
    g = client.get("/projects").json()
    assert g["project-b"]["priority"] == 99


def test_submit_with_requires_persists_and_returns(client):
    r = client.post(
        "/submit",
        json={
            "cmd": ["echo", "hi"],
            "cwd": "/tmp",
            "project": "project-a",
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
            "project": "project-a",
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
            "host": "server",
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
            "project": "project-b",
            "requires": {"needs": ["R"]},
        },
    )
    job_id = r.json()["id"]
    r = client.post(
        "/next-job",
        json={
            "host": "server",
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

    from sqlalchemy import update

    from jobd import app as app_mod

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
            "project": "project-a",
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


def test_sweeper_reclaim_clears_pending_signal(client):
    """M1: a job assigned to a dead worker with a pending cancel signal must be
    re-queued with signal cleared. Otherwise the fresh worker it re-dispatches
    to reads the stale 'cancel' on its first /signal poll and kills the re-run."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from jobd import app as app_mod
    from jobd.db import Worker

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
    job_id = client.post(
        "/submit", json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"}
    ).json()["id"]
    client.post(
        "/next-job",
        json={
            "host": "ghost",
            "free_vram_gb": 0,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 8,
            "idle_cpus": 4,
        },
    )
    # A cancel while ASSIGNED stamps a pending 'cancel' signal (worker not seen yet).
    client.post(f"/jobs/{job_id}/cancel")
    assert client.get(f"/jobs/{job_id}/signal").json()["signal"] == "cancel"

    # Worker goes silent past the reclaim threshold; sweep re-queues the job.
    engine = app_mod._engine_for_testing()
    with engine.begin() as conn:
        conn.execute(
            update(Worker)
            .where(Worker.host == "ghost")
            .values(last_heartbeat=datetime.now(UTC) - timedelta(minutes=6))
        )
    app_mod._sweep_once()

    got = client.get(f"/jobs/{job_id}").json()
    assert got["state"] == "queued", got
    # The stale signal must be gone so the re-dispatch isn't killed on poll.
    assert client.get(f"/jobs/{job_id}/signal").json()["signal"] is None


def _start_job_on_worker(client, host, job_json):
    """Helper: heartbeat a worker, submit a job, claim it, and POST /started so
    the job reaches RUNNING. Returns the job id."""
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
    r = client.post("/submit", json=job_json)
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    claim = client.post(
        "/next-job",
        json={
            "host": host,
            "free_vram_gb": 0,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 8,
            "idle_cpus": 4,
        },
    )
    assert claim.json()["id"] == job_id
    started = client.post(f"/jobs/{job_id}/started")
    assert started.json()["state"] == "running", started.text
    return job_id


def test_running_job_on_dead_worker_orphaned_and_cascades(client):
    """CRIT-2: a non-idempotent RUNNING job whose worker goes silent >5min is
    transitioned to ORPHANED (not left RUNNING forever), and its queued
    dependents are cascade-cancelled so /wait can return."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from jobd import app as app_mod
    from jobd.db import Worker

    parent_id = _start_job_on_worker(
        client, "ghost", {"cmd": ["true"], "cwd": "/tmp", "project": "project-a"}
    )
    # A child depending on the parent (default policy: needs parent COMPLETED).
    child = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-a",
            "depends_on": [parent_id],
        },
    )
    child_id = child.json()["id"]

    engine = app_mod._engine_for_testing()
    with engine.begin() as conn:
        conn.execute(
            update(Worker)
            .where(Worker.host == "ghost")
            .values(last_heartbeat=datetime.now(UTC) - timedelta(minutes=6))
        )

    app_mod._sweep_once()

    parent = client.get(f"/jobs/{parent_id}").json()
    assert parent["state"] == "orphaned", parent
    assert parent["termination_reason"] == "worker_died"
    assert parent["finished_at"] is not None
    child_got = client.get(f"/jobs/{child_id}").json()
    assert child_got["state"] == "cancelled", child_got


def test_running_idempotent_job_on_dead_worker_requeued(client):
    """CRIT-2: an idempotent RUNNING job is safe to re-run, so a dead worker
    (>90s for idempotent) requeues it rather than orphaning it."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from jobd import app as app_mod
    from jobd.db import Worker

    job_id = _start_job_on_worker(
        client,
        "ghost",
        {
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-a",
            "requires": {"idempotent": True},
        },
    )

    engine = app_mod._engine_for_testing()
    with engine.begin() as conn:
        conn.execute(
            update(Worker)
            .where(Worker.host == "ghost")
            .values(last_heartbeat=datetime.now(UTC) - timedelta(seconds=120))
        )

    app_mod._sweep_once()

    got = client.get(f"/jobs/{job_id}").json()
    assert got["state"] == "queued", got
    assert got["worker"] is None
    assert got["started_at"] is None


def test_running_job_past_wall_clock_orphaned_despite_live_worker(client):
    """RUNNING reaper wall-clock backstop: a job whose worker is still
    heartbeating but whose max_wall_s + grace has elapsed is orphaned with
    termination_reason='wall_clock_exceeded' — not left RUNNING forever.

    Mechanism: worker-side enforcement (SIGTERM at max_wall_s in
    job_worker.poll_signals) only lives in the worker's per-job monitor. If the
    worker crashes mid-run and restarts within DEAD_WORKER_SECONDS, the new
    worker has no memory of the job, never enforces its wall clock, and never
    posts /complete; the broker sees a live worker and leaves the job RUNNING
    forever. Regression for stranded job 1364 (desktop host-power crash
    2026-06-03)."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from jobd import app as app_mod
    from jobd.db import Job, Worker

    job_id = _start_job_on_worker(
        client,
        "alive",
        {"cmd": ["true"], "cwd": "/tmp", "project": "project-a", "max_wall_s": 900},
    )
    # A queued dependent so we also prove the wall-clock orphan cascades.
    child = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-a",
            "depends_on": [job_id],
        },
    )
    child_id = child.json()["id"]

    engine = app_mod._engine_for_testing()
    with engine.begin() as conn:
        # Worker is HEALTHY (fresh heartbeat) — the dead-worker reaper would NOT
        # fire. Only the wall-clock backstop should.
        conn.execute(
            update(Worker).where(Worker.host == "alive").values(last_heartbeat=datetime.now(UTC))
        )
        # Job started far past max_wall_s + grace (naive UTC, as stored).
        conn.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(started_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=4))
        )

    app_mod._sweep_once()

    got = client.get(f"/jobs/{job_id}").json()
    assert got["state"] == "orphaned", got
    assert got["termination_reason"] == "wall_clock_exceeded", got
    assert got["finished_at"] is not None
    child_got = client.get(f"/jobs/{child_id}").json()
    assert child_got["state"] == "cancelled", child_got


def test_running_job_within_wall_clock_left_running(client):
    """Anti-vacuity for the wall-clock backstop: a RUNNING job on a live worker
    that is still well within max_wall_s is left RUNNING (the backstop must not
    reap healthy in-progress jobs)."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from jobd import app as app_mod
    from jobd.db import Job, Worker

    job_id = _start_job_on_worker(
        client,
        "alive",
        {"cmd": ["true"], "cwd": "/tmp", "project": "project-a", "max_wall_s": 900},
    )
    engine = app_mod._engine_for_testing()
    with engine.begin() as conn:
        conn.execute(
            update(Worker).where(Worker.host == "alive").values(last_heartbeat=datetime.now(UTC))
        )
        conn.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(started_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=60))
        )

    app_mod._sweep_once()

    got = client.get(f"/jobs/{job_id}").json()
    assert got["state"] == "running", got


def test_submitted_env_round_trips_to_worker(client):
    """P0.3 (CRIT M1): env submitted with a job is stored and returned on the
    /next-job dispatch payload (and /jobs/{id}) so the worker can apply it.
    Previously env was stored but never returned, so --env silently no-op'd."""
    client.post(
        "/heartbeat",
        json={
            "host": "w1",
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
            "project": "project-a",
            "env": {"FOO": "bar", "BAZ": "qux"},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["env"] == {"FOO": "bar", "BAZ": "qux"}

    claim = client.post(
        "/next-job",
        json={
            "host": "w1",
            "free_vram_gb": 0,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 8,
            "idle_cpus": 4,
        },
    )
    assert claim.json()["env"] == {"FOO": "bar", "BAZ": "qux"}


def test_unmatcheable_job_gets_warning(client):
    """Job requiring arm64 with no arm64 worker advertising gets a warning.

    With #43 submit_preflight, this fires at submit time instead of 60s later
    in the sweeper. The sweeper-path remains the fallback when capabilities
    change AFTER submit (worker goes offline) — covered by other tests.
    """
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
            "project": "project-a",
            "requires": {"arch": "arm64"},
        },
    )
    body = r.json()
    assert body["warning"] is not None
    assert "unsatisfiable" in body["warning"]
    assert "arm64" in body["warning"]


def test_no_requires_job_never_gets_warning_on_empty_fleet(client):
    """A job with no requires block is matcheable by definition — empty fleet != mismatch."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from jobd import app as app_mod
    from jobd.db import Job

    # No heartbeats — empty fleet
    r = client.post(
        "/submit",
        json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"},
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

    from sqlalchemy import update

    from jobd import app as app_mod
    from jobd.db import Job

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
            "project": "project-a",
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

    from sqlalchemy import update

    from jobd import app as app_mod
    from jobd.db import Worker

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
            "project": "project-a",
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


def _submit(client, project="project-a", cmd=None, depends_on=None, any_exit=False):
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


def test_parent_failure_cascade_is_transitive(client):
    """A<-B<-C: when A fails via /complete, the cascade must cancel B AND
    transitively C (audit 2026-07-01 H2 — the old single-level cascade cancelled
    only the direct child B and stranded C in QUEUED forever)."""
    a = _submit(client).json()
    b = _submit(client, depends_on=[a["id"]]).json()
    c = _submit(client, depends_on=[b["id"]]).json()
    _heartbeat(client)
    _next_job(client)
    client.post(f"/jobs/{a['id']}/complete", json={"exit_code": 1, "final_state": "failed"})
    assert client.get(f"/jobs/{b['id']}").json()["state"] == "cancelled"
    assert client.get(f"/jobs/{c['id']}").json()["state"] == "cancelled"


def test_submit_rejects_default_dep_on_terminal_failed_parent(client):
    """audit 2026-07-01 H2: a default-policy dep on an ALREADY failed-side
    terminal parent can never be satisfied (the parent won't reach COMPLETED)
    and no future transition fires the cascade, so the child would strand in
    QUEUED forever. Reject at submit (400) instead."""
    parent = _submit(client).json()
    _heartbeat(client)
    _next_job(client)
    client.post(f"/jobs/{parent['id']}/complete", json={"exit_code": 1, "final_state": "failed"})
    assert client.get(f"/jobs/{parent['id']}").json()["state"] == "failed"

    r = _submit(client, depends_on=[parent["id"]])
    assert r.status_code == 400, r.text
    assert "failed-side terminal" in r.text


def test_submit_allows_any_exit_dep_on_terminal_parent(client):
    """The submit-time reject applies only to default-policy deps: an any-exit
    child is satisfied by any terminal parent, so it must still be accepted and
    become dispatchable immediately."""
    parent = _submit(client).json()
    _heartbeat(client)
    _next_job(client)
    client.post(f"/jobs/{parent['id']}/complete", json={"exit_code": 1, "final_state": "failed"})

    r = _submit(client, depends_on=[parent["id"]], any_exit=True)
    assert r.status_code == 200, r.text
    child = r.json()
    claim = _next_job(client).json()
    assert claim["id"] == child["id"]


def test_submit_allows_default_dep_on_completed_parent(client):
    """A default-policy dep on an already-COMPLETED parent is satisfied, not
    rejected — the reject targets only failed-side terminals."""
    parent = _submit(client).json()
    _heartbeat(client)
    _next_job(client)
    client.post(f"/jobs/{parent['id']}/complete", json={"exit_code": 0, "final_state": "completed"})

    r = _submit(client, depends_on=[parent["id"]])
    assert r.status_code == 200, r.text


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


def test_parent_preempted_cancels_child(client):
    """#35: PREEMPTED is treated as a failure-side terminal for the
    depends_on cascade — queued children without any_exit get cancelled,
    same as parent FAILED/CANCELLED. Aligns with the shipped path A
    semantics ("preempt = job dies, project rerun later"): a downstream
    job whose input never materialized cannot run.

    If a future user wants the chain to wait through preempt instead, they
    set `depends_on_any_exit=True` (covered by the next test)."""
    parent = _submit(client).json()
    child = _submit(client, depends_on=[parent["id"]]).json()
    _heartbeat(client)
    _next_job(client)
    client.post(f"/jobs/{parent['id']}/started")
    client.post(
        f"/jobs/{parent['id']}/complete",
        json={"exit_code": -15, "final_state": "preempted"},
    )
    parent_after = client.get(f"/jobs/{parent['id']}").json()
    assert parent_after["state"] == "preempted"
    row = client.get(f"/jobs/{child['id']}").json()
    assert row["state"] == "cancelled"
    assert "parent_failed" in (row.get("warning") or "")
    assert str(parent["id"]) in (row.get("warning") or "")


def test_parent_preempted_any_exit_keeps_child_queued(client):
    """#35: depends_on_any_exit=True opts a child out of the
    preempt-cascade; the child stays QUEUED and becomes runnable once the
    parent reaches any terminal. Use this when you'd rather have the
    pipeline limp forward on partial output than re-submit the chain."""
    parent = _submit(client).json()
    child = _submit(client, depends_on=[parent["id"]], any_exit=True).json()
    _heartbeat(client)
    _next_job(client)
    client.post(f"/jobs/{parent['id']}/started")
    client.post(
        f"/jobs/{parent['id']}/complete",
        json={"exit_code": -15, "final_state": "preempted"},
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


def test_complete_is_idempotent_on_terminal_job(client):
    """P3.2: a second /complete on an already-terminal job is a no-op — the
    recorded outcome is NOT overwritten (worker retries / late posts can't
    clobber a completed/cancelled job)."""
    job = _submit(client).json()
    _heartbeat(client)
    _next_job(client)
    client.post(f"/jobs/{job['id']}/started")
    r1 = client.post(
        f"/jobs/{job['id']}/complete",
        json={"exit_code": 0, "final_state": "completed"},
    )
    assert r1.json()["state"] == "completed"

    # A second, contradictory /complete must not change anything.
    r2 = client.post(
        f"/jobs/{job['id']}/complete",
        json={"exit_code": 1, "final_state": "failed"},
    )
    assert r2.status_code == 200
    assert r2.json()["state"] == "completed"
    assert r2.json()["exit_code"] == 0
    got = client.get(f"/jobs/{job['id']}").json()
    assert got["state"] == "completed" and got["exit_code"] == 0


def test_complete_rejects_invalid_final_state(client):
    """P3.2: a final_state that isn't a valid terminal state is rejected (400)
    rather than stored as a non-enum string that breaks later reads."""
    job = _submit(client).json()
    _heartbeat(client)
    _next_job(client)
    client.post(f"/jobs/{job['id']}/started")

    bogus = client.post(
        f"/jobs/{job['id']}/complete",
        json={"exit_code": 0, "final_state": "definitely-not-a-state"},
    )
    assert bogus.status_code == 400

    # A real JobState that isn't terminal is also rejected.
    nonterminal = client.post(
        f"/jobs/{job['id']}/complete",
        json={"exit_code": 0, "final_state": "running"},
    )
    assert nonterminal.status_code == 400

    # The job is untouched — still running.
    assert client.get(f"/jobs/{job['id']}").json()["state"] == "running"


def test_complete_rejects_stale_worker(client):
    """M2: after a partition reclaim + re-dispatch, the ORIGINAL worker (still
    running the workload) must not be able to terminal-ize the job that now
    belongs to a different worker. A /complete whose X-Jobd-Worker header
    doesn't match job.worker is refused (409); the job stays running."""
    job = _submit(client).json()
    _heartbeat(client, host="w1")
    _next_job(client, host="w1")  # job.worker = w1
    client.post(f"/jobs/{job['id']}/started", headers={"X-Jobd-Worker": "w1"})

    stale = client.post(
        f"/jobs/{job['id']}/complete",
        json={"exit_code": 0, "final_state": "completed"},
        headers={"X-Jobd-Worker": "w2"},  # a different, stale worker
    )
    assert stale.status_code == 409, stale.text
    assert client.get(f"/jobs/{job['id']}").json()["state"] == "running"

    # The owning worker completes it normally.
    ok = client.post(
        f"/jobs/{job['id']}/complete",
        json={"exit_code": 0, "final_state": "completed"},
        headers={"X-Jobd-Worker": "w1"},
    )
    assert ok.status_code == 200, ok.text
    assert client.get(f"/jobs/{job['id']}").json()["state"] == "completed"


def test_complete_without_worker_header_still_accepted(client):
    """Backward compatibility: a pre-header worker sends no X-Jobd-Worker, so the
    stale-worker check is skipped and /complete behaves as before."""
    job = _submit(client).json()
    _heartbeat(client, host="w1")
    _next_job(client, host="w1")
    r = client.post(
        f"/jobs/{job['id']}/complete", json={"exit_code": 0, "final_state": "completed"}
    )
    assert r.status_code == 200, r.text
    assert client.get(f"/jobs/{job['id']}").json()["state"] == "completed"


def test_log_rejects_stale_worker(client):
    """M2: a stale worker's /log chunk must not interleave into the log file of
    the run that now owns the job."""
    job = _submit(client).json()
    _heartbeat(client, host="w1")
    _next_job(client, host="w1")

    stale = client.post(
        f"/jobs/{job['id']}/log", content=b"stale bytes", headers={"X-Jobd-Worker": "w2"}
    )
    assert stale.status_code == 409, stale.text

    ok = client.post(
        f"/jobs/{job['id']}/log", content=b"real bytes", headers={"X-Jobd-Worker": "w1"}
    )
    assert ok.status_code == 200, ok.text
    out = client.get(f"/jobs/{job['id']}/output").json()
    assert out["tail"] == "real bytes"


def test_job_info_exposes_session_id(client):
    r = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-a",
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
            "cwd": "/mnt/c/Users/dev/some/project",
            "project": "project-a",
            "host_pin": "desktop-vm",
        },
    )
    assert r.status_code == 400
    assert "/mnt/c/" in r.text


def test_submit_allows_mnt_c_cwd_when_pinned_to_laptop(client):
    r = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/mnt/c/Users/dev/some/project",
            "project": "project-a",
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


def test_cancel_queued_loses_race_to_claim_falls_through_to_signal(client, monkeypatch):
    """H3 cancel-vs-claim TOCTOU. If a worker's atomic queued->assigned claim
    lands between cancel_job's read (which saw QUEUED) and its guarded write,
    the guarded UPDATE matches 0 rows. cancel must NOT clobber the claim to
    CANCELLED (which would strand the worker running a job the user was told
    was cancelled, with no kill signal). It must fall through to the signal
    path: state stays assigned, signal='cancel'.

    The race is injected deterministically: the first _cas_state call (the
    queued->cancelled CAS) flips the row to ASSIGNED via a real update and
    reports 0 rows, exactly as a concurrent claim committing mid-request would.
    """
    from jobd import app as app_mod
    from jobd.models import JobState

    job = _submit(client).json()
    real_cas = app_mod._cas_state
    calls = {"n": 0}

    def racing_cas(session, job_id, expected, **values):
        calls["n"] += 1
        if calls["n"] == 1 and values.get("state") == JobState.CANCELLED:
            # Simulate the matcher's claim winning: queued -> assigned, and
            # report that our guarded cancel matched nothing.
            real_cas(session, job_id, (JobState.QUEUED,), state=JobState.ASSIGNED, worker="w1")
            return 0
        return real_cas(session, job_id, expected, **values)

    monkeypatch.setattr(app_mod, "_cas_state", racing_cas)

    r = client.post(f"/jobs/{job['id']}/cancel")
    assert r.status_code == 200
    # Must NOT have been clobbered to cancelled.
    assert r.json()["state"] in ("assigned", "running"), r.json()
    # The cancel still lands — as a signal the worker honors.
    sig = client.get(f"/jobs/{job['id']}/signal").json()
    assert sig["signal"] == "cancel"


def test_started_transitions_assigned_to_running(client):
    """Worker POST /started flips assigned -> running so callers can observe
    the live in-flight phase. Without this, jobs spend their entire run in
    ASSIGNED and the MCP cancel signal_sent synthesis can't distinguish
    queued-cancel from SIGTERM-cancel."""
    job = _submit(client).json()
    _heartbeat(client)
    _next_job(client)
    assert client.get(f"/jobs/{job['id']}").json()["state"] == "assigned"

    r = client.post(f"/jobs/{job['id']}/started")
    assert r.status_code == 200
    assert r.json()["state"] == "running"


def test_started_is_idempotent_and_no_op_on_terminal(client):
    """Two /started POSTs leave the job in running. /started after /complete
    is a no-op (state stays terminal). The worker may retry /started on a
    transient network error; the broker must not re-open a finished job."""
    job = _submit(client).json()
    _heartbeat(client)
    _next_job(client)
    client.post(f"/jobs/{job['id']}/started")
    client.post(f"/jobs/{job['id']}/started")
    assert client.get(f"/jobs/{job['id']}").json()["state"] == "running"

    client.post(f"/jobs/{job['id']}/complete", json={"exit_code": 0})
    client.post(f"/jobs/{job['id']}/started")
    after = client.get(f"/jobs/{job['id']}").json()
    assert after["state"] == "completed"


def test_started_404_on_unknown_job(client):
    r = client.post("/jobs/9999999/started")
    assert r.status_code == 404


def test_submit_fast_path_profile_stamps_flag(client):
    """Profiles declaring fast_path: true must propagate to the job so the
    worker can skip heavy-run wrapping. Without this, the flag lives only
    in profiles.yaml and nobody reads it."""
    r = client.post(
        "/submit",
        json={
            "cmd": ["echo", "hi"],
            "cwd": "/tmp",
            "project": "project-a",
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
            "project": "project-a",
            "profile": "gpu-heavy",
        },
    )
    assert r.status_code == 200
    assert r.json()["fast_path"] is False


def test_submit_no_profile_defaults_fast_path_false(client):
    r = client.post(
        "/submit",
        json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"},
    )
    assert r.json()["fast_path"] is False


def test_submit_fast_path_request_field_honored_without_profile(client):
    """fast_path=true in the submit body must propagate to the job even when no
    profile is named. Without this, every fast-path caller is forced to define
    a profile just to flip the flag — which is what jobd-mcp ran into."""
    r = client.post(
        "/submit",
        json={
            "cmd": ["echo", "x"],
            "cwd": "/tmp",
            "project": "project-a",
            "fast_path": True,
        },
    )
    assert r.status_code == 200
    assert r.json()["fast_path"] is True


def test_submit_fast_path_request_field_overrides_profile_false(client):
    """An explicit fast_path=true on the request beats a profile that says
    fast_path=false. Submit-time signal is more specific than the profile."""
    r = client.post(
        "/submit",
        json={
            "cmd": ["bash", "train.sh"],
            "cwd": "/tmp",
            "project": "project-a",
            "profile": "gpu-heavy",
            "fast_path": True,
        },
    )
    assert r.status_code == 200
    assert r.json()["fast_path"] is True


def test_submit_fast_path_request_field_can_disable_profile_true(client):
    """The reverse: fast_path=false at submit beats profile.fast_path=true."""
    r = client.post(
        "/submit",
        json={
            "cmd": ["echo", "hi"],
            "cwd": "/tmp",
            "project": "project-a",
            "profile": "small",
            "fast_path": False,
        },
    )
    assert r.status_code == 200
    assert r.json()["fast_path"] is False


def test_submit_max_wall_and_idle_timeout_round_trip(client):
    """The two watchdog fields persist through submit and come back on read.
    Worker-side enforcement is covered separately in test_worker."""
    r = client.post(
        "/submit",
        json={
            "cmd": ["echo", "hi"],
            "cwd": "/tmp",
            "project": "project-a",
            "max_wall_s": 3600,
            "idle_timeout_s": 300,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["max_wall_s"] == 3600
    assert body["idle_timeout_s"] == 300
    assert body["termination_reason"] is None


def test_submit_rejects_negative_timeouts(client):
    """Pydantic ge=1 constraint must reject zero/negative — there's no
    sensible meaning to 'timeout in 0 seconds'."""
    r = client.post(
        "/submit",
        json={
            "cmd": ["echo"],
            "cwd": "/tmp",
            "project": "project-a",
            "max_wall_s": 0,
        },
    )
    assert r.status_code == 422


def test_submit_omitted_timeouts_default_to_null(client):
    """When neither timeout is specified, both come back null. Worker reads
    null as 'no per-job override; fall through to env default if any'."""
    r = client.post(
        "/submit",
        json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"},
    )
    body = r.json()
    assert body["max_wall_s"] is None
    assert body["idle_timeout_s"] is None


def test_complete_persists_termination_reason(client):
    """Worker can pass termination_reason in the /complete payload; it
    persists and surfaces in JobInfo. Watchdog kills set this so callers
    can distinguish 'failed because exit 1' from 'failed because hung'."""
    r = client.post(
        "/submit",
        json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"},
    )
    job_id = r.json()["id"]
    # Need to dispatch first so it's in a transitional state, then complete.
    # Easier: hand-flip via an existing path — POST /complete directly works
    # for any job state in this codebase (see complete_job).
    r2 = client.post(
        f"/jobs/{job_id}/complete",
        json={"exit_code": -15, "final_state": "failed", "termination_reason": "idle_timeout"},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["termination_reason"] == "idle_timeout"
    assert body["state"] == "failed"


def test_submit_fast_path_omitted_falls_through_to_profile(client):
    """When fast_path isn't specified at submit, the profile's value still wins.
    Backward-compat with existing callers that rely on profile-derived flag."""
    r = client.post(
        "/submit",
        json={
            "cmd": ["echo", "hi"],
            "cwd": "/tmp",
            "project": "project-a",
            "profile": "small",
        },
    )
    assert r.status_code == 200
    assert r.json()["fast_path"] is True


def test_heartbeat_persists_mount_roots_and_workers_returns_them(client):
    client.post(
        "/heartbeat",
        json={
            "host": "desktop-vm",
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
    rows = [w for w in r.json() if w["host"] == "desktop-vm"]
    assert rows and rows[0]["mount_roots"] == ["/home", "/mnt/d", "/tmp"]


def test_next_job_skips_job_with_cwd_outside_worker_mount_roots(client):
    """Worker that advertises mount_roots must not receive jobs whose cwd
    isn't under any of them — otherwise the job fails at subprocess spawn
    with cwd-does-not-exist."""
    client.post(
        "/heartbeat",
        json={
            "host": "desktop-vm",
            "free_vram_gb": 20,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 30,
            "idle_cpus": 8,
            "host_aliases": [],
            "mount_roots": ["/home", "/tmp"],
        },
    )
    # A second worker DOES cover /mnt/d so the submit is routable (the realistic
    # fleet state). The submit-time cwd_routability probe hard-denies an any-pin
    # cwd that NO known worker covers; here laptop covers it, so submit succeeds
    # and we exercise the /next-job filter for the non-covering desktop-vm.
    client.post(
        "/heartbeat",
        json={
            "host": "laptop",
            "free_vram_gb": 20,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 30,
            "idle_cpus": 8,
            "host_aliases": [],
            "mount_roots": ["/mnt/d"],
        },
    )
    r = client.post(
        "/submit",
        json={
            "cmd": ["ls"],
            "cwd": "/mnt/d/some/project",
            "project": "project-a",
            "host_pin": "any",
        },
    )
    assert r.status_code == 200
    got = client.post(
        "/next-job",
        json={
            "host": "desktop-vm",
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
            "project": "project-a",
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
        json={"cmd": ["ls"], "cwd": "/home/user/proj", "project": "demo-project"},
    ).json()
    job_d = client.post(
        "/submit",
        json={"cmd": ["ls"], "cwd": "/mnt/d/data", "project": "project-a"},
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


# ---------- scheduler-awareness warnings (#38 blocked-by-load, #39 single-slot) ----------


def _heartbeat_caps(client, host: str, **kwargs):
    """Heartbeat with default linux/x86_64/no-gpu unless overridden."""
    body = {
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
    body.update(kwargs)
    client.post("/heartbeat", json=body)


def test_submit_warns_when_only_eligible_worker_is_busy(client):
    """#39: one host satisfies caps + host_pin AND has a job in flight → warning."""
    _heartbeat_caps(client, "solo")
    first = client.post(
        "/submit",
        json={"cmd": ["sleep", "60"], "cwd": "/tmp", "project": "project-a"},
    ).json()
    # Claim it so 'solo' is busy
    claimed = _next_job(client, host="solo").json()
    assert claimed["id"] == first["id"]

    second = client.post(
        "/submit",
        json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"},
    ).json()
    assert second["warning"] is not None
    assert "will queue behind" in second["warning"]
    assert f"job {first['id']}" in second["warning"]
    assert "solo" in second["warning"]


def test_submit_no_warning_when_multiple_eligible_workers(client):
    """#39: with two eligible hosts, even if one is busy, no single-slot warning."""
    _heartbeat_caps(client, "host-a")
    _heartbeat_caps(client, "host-b")
    first = client.post(
        "/submit",
        json={"cmd": ["sleep", "60"], "cwd": "/tmp", "project": "project-a"},
    ).json()
    _next_job(client, host="host-a")  # busy 'host-a'

    second = client.post(
        "/submit",
        json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"},
    ).json()
    assert second["warning"] is None
    assert first["id"] != second["id"]


def test_submit_no_warning_when_only_worker_is_idle(client):
    """#39: one host but idle → no warning (the matcher will pick it next poll)."""
    _heartbeat_caps(client, "solo")
    j = client.post(
        "/submit",
        json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"},
    ).json()
    assert j["warning"] is None


def test_submit_preflight_unknown_host_pin_typo(client):
    """#43: `--host desktop-vm` (typo for `desktop`) gets a submit-time warning,
    not a 60s-later sweeper warning. Catches the silent-queue-forever class."""
    _heartbeat_caps(client, "desktop")
    _heartbeat_caps(client, "laptop")
    r = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-a",
            "host_pin": "desktop-vm",
        },
    )
    body = r.json()
    assert body["warning"] is not None
    assert "desktop-vm" in body["warning"]
    assert "unsatisfiable host_pin" in body["warning"]


def test_submit_preflight_unmet_needs_tag(client):
    """#43: `--needs nonexistent-tag` flagged at submit, not 60s later."""
    _heartbeat_caps(client, "desktop", tags=["python3"])
    r = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-a",
            "requires": {"needs": ["nonexistent-tag"]},
        },
    )
    body = r.json()
    assert body["warning"] is not None
    assert "nonexistent-tag" in body["warning"]


def test_submit_preflight_offline_worker_satisfies_pin(client):
    """A worker that's been seen but is offline still represents fleet capability —
    don't false-positive a typo when desktop is rebooting."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from jobd import app as app_mod
    from jobd.db import Worker

    _heartbeat_caps(client, "desktop")
    # Mark offline by aging last_heartbeat past OFFLINE_AFTER_SECONDS.
    engine = app_mod._engine_for_testing()
    with engine.begin() as conn:
        conn.execute(
            update(Worker)
            .where(Worker.host == "desktop")
            .values(
                state="offline",
                last_heartbeat=datetime.now(UTC) - timedelta(seconds=600),
            )
        )
    r = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-a",
            "host_pin": "desktop",
        },
    )
    body = r.json()
    # Either no warning, or only "will queue behind" / contention warning —
    # never an "unsatisfiable host_pin" claim against a known offline worker.
    if body["warning"] is not None:
        assert "unsatisfiable host_pin" not in body["warning"]


def test_submit_preflight_silent_when_satisfiable(client):
    _heartbeat_caps(client, "desktop", tags=["cuda-32gb"], gpu=True, free_vram_gb=24)
    r = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-a",
            "requires": {"gpu": True, "needs": ["cuda-32gb"]},
        },
    )
    body = r.json()
    if body["warning"] is not None:
        assert "unsatisfiable" not in body["warning"]


def test_submit_warning_respects_host_pin(client):
    """#39: a host_pin'd job sees only matching hosts as eligible."""
    _heartbeat_caps(client, "host-a")
    _heartbeat_caps(client, "host-b")
    # Pin to host-a, busy host-a
    first = client.post(
        "/submit",
        json={
            "cmd": ["sleep", "60"],
            "cwd": "/tmp",
            "project": "project-a",
            "host_pin": "host-a",
        },
    ).json()
    _next_job(client, host="host-a")

    pinned = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-a",
            "host_pin": "host-a",
        },
    ).json()
    assert pinned["warning"] is not None
    assert "will queue behind" in pinned["warning"]
    assert "host-a" in pinned["warning"]
    assert first["id"] != pinned["id"]


# --- #42 Fix A: GPU contention warning ---


def test_submit_gpu_contention_warning_fires_when_all_gpu_workers_saturated(client):
    """A `--gpu` job submitted when every GPU worker has foreign processes
    holding most VRAM gets a warning that names the saturated hosts."""
    _heartbeat_caps(
        client,
        "desktop",
        gpu=True,
        free_vram_gb=16.0,
        unregistered_vram_gb=16.0,
        tags=["cuda"],
    )
    _heartbeat_caps(
        client,
        "laptop",
        gpu=True,
        free_vram_gb=12.0,
        unregistered_vram_gb=11.0,
        tags=["cuda"],
    )
    j = client.post(
        "/submit",
        json={
            "cmd": ["nvidia-smi"],
            "cwd": "/tmp",
            "project": "project-a",
            "requires": {"gpu": True},
        },
    ).json()
    assert j["warning"] is not None
    assert "GPU contention" in j["warning"]
    assert "desktop" in j["warning"]
    assert "laptop" in j["warning"]


def test_submit_gpu_contention_silent_when_one_worker_has_headroom(client):
    """At least one GPU host with VRAM headroom → no contention warning
    (the matcher will route there)."""
    _heartbeat_caps(
        client,
        "desktop",
        gpu=True,
        free_vram_gb=16.0,
        unregistered_vram_gb=16.0,
        tags=["cuda"],
    )
    _heartbeat_caps(
        client,
        "laptop",
        gpu=True,
        free_vram_gb=11.0,
        unregistered_vram_gb=0.0,
        tags=["cuda"],
    )
    j = client.post(
        "/submit",
        json={
            "cmd": ["nvidia-smi"],
            "cwd": "/tmp",
            "project": "project-a",
            "requires": {"gpu": True},
        },
    ).json()
    assert j["warning"] is None


def test_submit_gpu_contention_silent_when_no_gpu_required(client):
    """A CPU-only job is unaffected by GPU foreign-process contention."""
    _heartbeat_caps(
        client,
        "desktop",
        gpu=True,
        free_vram_gb=16.0,
        unregistered_vram_gb=16.0,
        tags=["cuda"],
    )
    j = client.post(
        "/submit",
        json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"},
    ).json()
    assert j["warning"] is None


def test_sweep_blocked_warning_after_5min_with_nonpreemptible_blocker(client):
    """#38: queued >5min with all eligible workers running non-preemptible jobs."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from jobd import app as app_mod
    from jobd.db import Job

    _heartbeat_caps(client, "solo")
    blocker = client.post(
        "/submit",
        json={
            "cmd": ["sleep", "9999"],
            "cwd": "/tmp",
            "project": "project-a",
            "preemptible": False,
        },
    ).json()
    _next_job(client, host="solo")  # claim it

    queued = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-a",
            "scheduling_timeout_s": 7 * 24 * 3600,
        },
    ).json()

    # Backdate submitted_at to >5min ago
    engine = app_mod._engine_for_testing()
    with engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == queued["id"])
            .values(submitted_at=datetime.now(UTC) - timedelta(seconds=400))
        )
    app_mod._sweep_once()
    got = client.get(f"/jobs/{queued['id']}").json()
    assert got["warning"] is not None
    assert got["warning"].startswith("queue-age ")
    assert f"job {blocker['id']}" in got["warning"]
    assert "preempt" in got["warning"]


def test_sweep_auto_preempts_when_blocker_is_preemptible(client):
    """#8 path A: a preemptible blocker on the only eligible worker is
    auto-preempted in favor of a higher-priority queued job past the
    queue-age threshold. The blocker's `signal` flips to 'preempt' and the
    queued job stays warning-free for that path (no nonpreemptible-blocker
    string)."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from jobd import app as app_mod
    from jobd.db import Job

    _heartbeat_caps(client, "solo")
    blocker = client.post(
        "/submit",
        json={
            "cmd": ["sleep", "9999"],
            "cwd": "/tmp",
            "project": "project-a",
            "preemptible": True,
        },
    ).json()
    _next_job(client, host="solo")
    client.post(f"/jobs/{blocker['id']}/started")

    queued = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-a",
            "scheduling_timeout_s": 7 * 24 * 3600,
        },
    ).json()

    engine = app_mod._engine_for_testing()
    runtime_floor = app_mod.AUTO_PREEMPT_MIN_RUNTIME_SECONDS
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == blocker["id"])
            .values(
                priority=30,
                started_at=now - timedelta(seconds=runtime_floor + 60),
            )
        )
        conn.execute(
            update(Job)
            .where(Job.id == queued["id"])
            .values(priority=90, submitted_at=now - timedelta(seconds=400))
        )
    app_mod._sweep_once()

    sig = client.get(f"/jobs/{blocker['id']}/signal").json()
    assert sig["signal"] == "preempt"
    blocker_after = client.get(f"/jobs/{blocker['id']}").json()
    assert blocker_after["warning"] is not None
    assert blocker_after["warning"].startswith("auto-preempted in favor of job ")
    assert str(queued["id"]) in blocker_after["warning"]

    queued_after = client.get(f"/jobs/{queued['id']}").json()
    if queued_after["warning"] is not None:
        assert not queued_after["warning"].startswith("queue-age ")


def test_sweep_no_auto_preempt_when_blocker_runtime_below_floor(client):
    """Blocker started <AUTO_PREEMPT_MIN_RUNTIME_SECONDS ago is protected;
    the sweeper does not signal it. Avoids trashing freshly-started work."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from jobd import app as app_mod
    from jobd.db import Job

    _heartbeat_caps(client, "solo")
    blocker = client.post(
        "/submit",
        json={
            "cmd": ["sleep", "9999"],
            "cwd": "/tmp",
            "project": "project-a",
            "preemptible": True,
        },
    ).json()
    _next_job(client, host="solo")
    client.post(f"/jobs/{blocker['id']}/started")

    queued = client.post(
        "/submit",
        json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"},
    ).json()

    engine = app_mod._engine_for_testing()
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == blocker["id"])
            .values(priority=30, started_at=now - timedelta(seconds=10))
        )
        conn.execute(
            update(Job)
            .where(Job.id == queued["id"])
            .values(priority=90, submitted_at=now - timedelta(seconds=400))
        )
    app_mod._sweep_once()

    sig = client.get(f"/jobs/{blocker['id']}/signal").json()
    assert sig["signal"] is None


def test_sweep_no_auto_preempt_when_blocker_priority_not_lower(client):
    """A preemptible blocker at equal-or-higher priority than the queued
    job is protected — auto-preempt only fires when the queued job is
    strictly higher priority."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from jobd import app as app_mod
    from jobd.db import Job

    _heartbeat_caps(client, "solo")
    blocker = client.post(
        "/submit",
        json={
            "cmd": ["sleep", "9999"],
            "cwd": "/tmp",
            "project": "project-a",
            "preemptible": True,
        },
    ).json()
    _next_job(client, host="solo")
    client.post(f"/jobs/{blocker['id']}/started")

    queued = client.post(
        "/submit",
        json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"},
    ).json()

    engine = app_mod._engine_for_testing()
    runtime_floor = app_mod.AUTO_PREEMPT_MIN_RUNTIME_SECONDS
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == blocker["id"])
            .values(
                priority=80,
                started_at=now - timedelta(seconds=runtime_floor + 60),
            )
        )
        conn.execute(
            update(Job)
            .where(Job.id == queued["id"])
            .values(priority=50, submitted_at=now - timedelta(seconds=400))
        )
    app_mod._sweep_once()

    sig = client.get(f"/jobs/{blocker['id']}/signal").json()
    assert sig["signal"] is None


def test_sweep_auto_preempt_does_not_double_fire(client):
    """A blocker that already has signal='preempt' from a prior sweeper
    pass is not re-signaled; warning is left alone. Idempotency under
    repeated sweeper passes."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from jobd import app as app_mod
    from jobd.db import Job

    _heartbeat_caps(client, "solo")
    blocker = client.post(
        "/submit",
        json={
            "cmd": ["sleep", "9999"],
            "cwd": "/tmp",
            "project": "project-a",
            "preemptible": True,
        },
    ).json()
    _next_job(client, host="solo")
    client.post(f"/jobs/{blocker['id']}/started")

    queued = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-a",
            "scheduling_timeout_s": 7 * 24 * 3600,
        },
    ).json()

    engine = app_mod._engine_for_testing()
    runtime_floor = app_mod.AUTO_PREEMPT_MIN_RUNTIME_SECONDS
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == blocker["id"])
            .values(
                priority=30,
                started_at=now - timedelta(seconds=runtime_floor + 60),
            )
        )
        conn.execute(
            update(Job)
            .where(Job.id == queued["id"])
            .values(priority=90, submitted_at=now - timedelta(seconds=400))
        )
    app_mod._sweep_once()
    first_warning = client.get(f"/jobs/{blocker['id']}").json()["warning"]
    assert first_warning is not None and first_warning.startswith("auto-preempted in favor of job ")

    # Second sweep — signal already set, should not change warning text
    app_mod._sweep_once()
    second_warning = client.get(f"/jobs/{blocker['id']}").json()["warning"]
    assert second_warning == first_warning


def test_preempt_endpoint_409_when_not_running(client):
    """POST /jobs/{id}/preempt refuses with 409 on queued/terminal jobs."""
    job = _submit(client).json()
    r = client.post(f"/jobs/{job['id']}/preempt")
    assert r.status_code == 409
    assert "queued" in r.json()["detail"]


def test_preempt_endpoint_409_when_not_preemptible(client):
    """POST /jobs/{id}/preempt refuses non-preemptible jobs even if running."""
    _heartbeat(client)
    job = client.post(
        "/submit",
        json={
            "cmd": ["sleep", "999"],
            "cwd": "/tmp",
            "project": "project-a",
            "preemptible": False,
        },
    ).json()
    _next_job(client)
    client.post(f"/jobs/{job['id']}/started")

    r = client.post(f"/jobs/{job['id']}/preempt")
    assert r.status_code == 409
    assert "not preemptible" in r.json()["detail"]


def test_preempt_endpoint_404_unknown_job(client):
    r = client.post("/jobs/9999999/preempt")
    assert r.status_code == 404


def test_preempt_endpoint_running_sets_signal(client):
    """POST /preempt on a running preemptible job sets job.signal='preempt'
    and leaves state assigned/running — worker SIGTERMs and finishes the
    transition (mirrors cancel's async contract)."""
    _heartbeat(client)
    job = client.post(
        "/submit",
        json={
            "cmd": ["sleep", "999"],
            "cwd": "/tmp",
            "project": "project-a",
            "preemptible": True,
        },
    ).json()
    _next_job(client)
    client.post(f"/jobs/{job['id']}/started")

    r = client.post(f"/jobs/{job['id']}/preempt")
    assert r.status_code == 200
    row = r.json()
    assert row["state"] in ("assigned", "running")

    sig = client.get(f"/jobs/{job['id']}/signal").json()
    assert sig["signal"] == "preempt"


def test_preempt_endpoint_assigned_before_started_sets_signal(client):
    """#34: between `/next-job` (state -> ASSIGNED) and the worker's
    `/started` POST (state -> RUNNING), there's a real-world window. The
    endpoint must accept that window: signal=preempt is queued so the
    worker sees it on its first poll after subprocess launch."""
    _heartbeat(client)
    job = client.post(
        "/submit",
        json={
            "cmd": ["sleep", "999"],
            "cwd": "/tmp",
            "project": "project-a",
            "preemptible": True,
        },
    ).json()
    _next_job(client)  # QUEUED -> ASSIGNED, NO /started

    pre = client.get(f"/jobs/{job['id']}").json()
    assert pre["state"] == "assigned"

    r = client.post(f"/jobs/{job['id']}/preempt")
    assert r.status_code == 200
    assert r.json()["state"] == "assigned"  # state untouched; signal queued

    sig = client.get(f"/jobs/{job['id']}/signal").json()
    assert sig["signal"] == "preempt"


def test_preempt_blockers_signals_lower_priority_blocker(client):
    """`POST /jobs/{id}/preempt-blockers` finds a preemptible blocker on
    the queued job's eligible workers and signals it immediately — no
    queue-age or runtime guards."""
    from sqlalchemy import update

    from jobd import app as app_mod
    from jobd.db import Job

    _heartbeat_caps(client, "solo")
    blocker = client.post(
        "/submit",
        json={
            "cmd": ["sleep", "9999"],
            "cwd": "/tmp",
            "project": "project-a",
            "preemptible": True,
        },
    ).json()
    _next_job(client, host="solo")
    client.post(f"/jobs/{blocker['id']}/started")

    queued = client.post(
        "/submit",
        json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"},
    ).json()

    engine = app_mod._engine_for_testing()
    with engine.begin() as conn:
        conn.execute(update(Job).where(Job.id == blocker["id"]).values(priority=30))
        conn.execute(update(Job).where(Job.id == queued["id"]).values(priority=90))

    r = client.post(f"/jobs/{queued['id']}/preempt-blockers")
    assert r.status_code == 200
    body = r.json()
    assert body["signaled"] == blocker["id"]
    assert body["worker"] == "solo"

    sig = client.get(f"/jobs/{blocker['id']}/signal").json()
    assert sig["signal"] == "preempt"
    blocker_after = client.get(f"/jobs/{blocker['id']}").json()
    assert "(manual)" in (blocker_after["warning"] or "")


def test_preempt_blockers_409_when_not_queued(client):
    """Refuses with 409 when target job is not in queued state."""
    _heartbeat(client)
    job = client.post(
        "/submit",
        json={
            "cmd": ["sleep", "999"],
            "cwd": "/tmp",
            "project": "project-a",
            "preemptible": True,
        },
    ).json()
    _next_job(client)
    client.post(f"/jobs/{job['id']}/started")

    r = client.post(f"/jobs/{job['id']}/preempt-blockers")
    assert r.status_code == 409
    assert "queued" in r.json()["detail"]


def test_preempt_blockers_no_candidate_returns_reason(client):
    """If no preemptible blocker at lower priority qualifies, returns
    `signaled=null` with a reason explaining why."""
    from sqlalchemy import update

    from jobd import app as app_mod
    from jobd.db import Job

    _heartbeat_caps(client, "solo")
    blocker = client.post(
        "/submit",
        json={
            "cmd": ["sleep", "9999"],
            "cwd": "/tmp",
            "project": "project-a",
            "preemptible": True,
        },
    ).json()
    _next_job(client, host="solo")
    client.post(f"/jobs/{blocker['id']}/started")

    queued = client.post(
        "/submit",
        json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"},
    ).json()

    # Make the queued job's priority NOT exceed the blocker's
    engine = app_mod._engine_for_testing()
    with engine.begin() as conn:
        conn.execute(update(Job).where(Job.id == blocker["id"]).values(priority=80))
        conn.execute(update(Job).where(Job.id == queued["id"]).values(priority=50))

    r = client.post(f"/jobs/{queued['id']}/preempt-blockers")
    assert r.status_code == 200
    body = r.json()
    assert body["signaled"] is None
    assert "lower priority" in body["reason"]


def test_preempt_blockers_force_overrides_priority_guard(client):
    """`force=true` drops the priority guard, letting an operator preempt
    blockers at equal-or-higher priority."""
    from sqlalchemy import update

    from jobd import app as app_mod
    from jobd.db import Job

    _heartbeat_caps(client, "solo")
    blocker = client.post(
        "/submit",
        json={
            "cmd": ["sleep", "9999"],
            "cwd": "/tmp",
            "project": "project-a",
            "preemptible": True,
        },
    ).json()
    _next_job(client, host="solo")
    client.post(f"/jobs/{blocker['id']}/started")

    queued = client.post(
        "/submit",
        json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"},
    ).json()

    engine = app_mod._engine_for_testing()
    with engine.begin() as conn:
        conn.execute(update(Job).where(Job.id == blocker["id"]).values(priority=80))
        conn.execute(update(Job).where(Job.id == queued["id"]).values(priority=50))

    r = client.post(f"/jobs/{queued['id']}/preempt-blockers", params={"force": "true"})
    assert r.status_code == 200
    body = r.json()
    assert body["signaled"] == blocker["id"]


def test_list_jobs_warnings_only_filters(client):
    """`GET /jobs?warnings_only=true` returns only jobs with non-null
    warning. Jobs without warnings drop out of the result."""
    from sqlalchemy import update

    from jobd import app as app_mod
    from jobd.db import Job

    _heartbeat(client)
    no_warn = _submit(client).json()
    with_warn = _submit(client).json()
    engine = app_mod._engine_for_testing()
    with engine.begin() as conn:
        conn.execute(update(Job).where(Job.id == with_warn["id"]).values(warning="test marker"))

    all_rows = client.get("/jobs").json()
    ids_all = {j["id"] for j in all_rows}
    assert {no_warn["id"], with_warn["id"]} <= ids_all

    only = client.get("/jobs", params={"warnings_only": "true"}).json()
    ids_only = {j["id"] for j in only}
    assert with_warn["id"] in ids_only
    assert no_warn["id"] not in ids_only


def test_sweep_clears_blocked_warning_when_blocker_finishes(client):
    """#38: once the non-preemptible blocker completes, the queue-age warning clears."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from jobd import app as app_mod
    from jobd.db import Job

    _heartbeat_caps(client, "solo")
    blocker = client.post(
        "/submit",
        json={
            "cmd": ["sleep", "9999"],
            "cwd": "/tmp",
            "project": "project-a",
            "preemptible": False,
        },
    ).json()
    _next_job(client, host="solo")

    queued = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-a",
            "scheduling_timeout_s": 7 * 24 * 3600,
        },
    ).json()

    engine = app_mod._engine_for_testing()
    with engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == queued["id"])
            .values(submitted_at=datetime.now(UTC) - timedelta(seconds=400))
        )
    app_mod._sweep_once()
    assert client.get(f"/jobs/{queued['id']}").json()["warning"].startswith("queue-age ")

    # Blocker finishes
    client.post(f"/jobs/{blocker['id']}/complete", json={"exit_code": 0})
    app_mod._sweep_once()
    got = client.get(f"/jobs/{queued['id']}").json()
    assert got["warning"] is None or not got["warning"].startswith("queue-age ")


def test_sweep_preserves_will_queue_behind_warning(client):
    """The matcheable-clear path must not wipe submit-time 'will queue behind' warnings."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from jobd import app as app_mod
    from jobd.db import Job

    _heartbeat_caps(client, "solo")
    client.post(
        "/submit",
        json={"cmd": ["sleep", "60"], "cwd": "/tmp", "project": "project-a"},
    )
    _next_job(client, host="solo")

    queued = client.post(
        "/submit",
        json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"},
    ).json()
    assert "will queue behind" in queued["warning"]

    # Push past UNMATCHEABLE threshold but well under the 5min blocked threshold.
    engine = app_mod._engine_for_testing()
    with engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == queued["id"])
            .values(submitted_at=datetime.now(UTC) - timedelta(seconds=90))
        )
    app_mod._sweep_once()
    got = client.get(f"/jobs/{queued['id']}").json()
    assert got["warning"] is not None
    assert "will queue behind" in got["warning"]


def _read_events(tmp_path) -> list[dict]:
    import json

    p = tmp_path / "logs" / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_auto_preempt_emits_jsonl_event_from_sweeper(client, tmp_path):
    """#32: when the sweeper auto-preempts, an `auto_preempt` event with
    source='sweeper' lands in events.jsonl with both job IDs, the worker,
    and queue-age."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from jobd import app as app_mod
    from jobd.db import Job

    _heartbeat_caps(client, "solo")
    blocker = client.post(
        "/submit",
        json={
            "cmd": ["sleep", "9999"],
            "cwd": "/tmp",
            "project": "project-a",
            "preemptible": True,
        },
    ).json()
    _next_job(client, host="solo")
    client.post(f"/jobs/{blocker['id']}/started")
    queued = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-a",
            "scheduling_timeout_s": 7 * 24 * 3600,
        },
    ).json()

    engine = app_mod._engine_for_testing()
    runtime_floor = app_mod.AUTO_PREEMPT_MIN_RUNTIME_SECONDS
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == blocker["id"])
            .values(priority=30, started_at=now - timedelta(seconds=runtime_floor + 60))
        )
        conn.execute(
            update(Job)
            .where(Job.id == queued["id"])
            .values(priority=90, submitted_at=now - timedelta(seconds=400))
        )
    app_mod._sweep_once()

    events = [e for e in _read_events(tmp_path) if e["event"] == "auto_preempt"]
    assert len(events) == 1, events
    e = events[0]
    assert e["source"] == "broker"
    assert e["job_id"] == blocker["id"]
    assert e["project"] == "project-a"
    p = e["payload"]
    assert p["cause"] == "sweeper"
    assert p["queued_job"] == queued["id"]
    assert p["worker"] == "solo"
    assert p["queued_priority"] == 90
    assert p["candidate_priority"] == 30
    assert p["queue_age_s"] >= 400


def test_preempt_blockers_emits_jsonl_event(client, tmp_path):
    """#32: manual `preempt-blockers` escalation also writes an
    `auto_preempt` event, distinguished by source='manual' (or
    'manual_force' with --force)."""
    from sqlalchemy import update

    from jobd import app as app_mod
    from jobd.db import Job

    _heartbeat_caps(client, "solo")
    blocker = client.post(
        "/submit",
        json={
            "cmd": ["sleep", "9999"],
            "cwd": "/tmp",
            "project": "project-a",
            "preemptible": True,
        },
    ).json()
    _next_job(client, host="solo")
    client.post(f"/jobs/{blocker['id']}/started")
    queued = client.post(
        "/submit", json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"}
    ).json()

    engine = app_mod._engine_for_testing()
    with engine.begin() as conn:
        conn.execute(update(Job).where(Job.id == blocker["id"]).values(priority=30))
        conn.execute(update(Job).where(Job.id == queued["id"]).values(priority=90))

    client.post(f"/jobs/{queued['id']}/preempt-blockers")
    client.post(
        f"/jobs/{queued['id']}/preempt-blockers", params={"force": "true"}
    )  # second call: candidate already signaled, returns no candidate; no event

    events = [e for e in _read_events(tmp_path) if e["event"] == "auto_preempt"]
    assert len(events) == 1, events
    assert events[0]["source"] == "broker"
    assert events[0]["job_id"] == blocker["id"]
    assert events[0]["project"] == "project-a"
    assert events[0]["payload"]["cause"] == "manual"
    assert events[0]["payload"]["queued_job"] == queued["id"]
    assert events[0]["payload"]["worker"] == "solo"


def test_checkpoint_complete_endpoint_emits_event(client, tmp_path):
    """#37: workers POST /jobs/{id}/checkpoint-complete after observing
    the user's `jobd-checkpoint-complete` token in stdout during a
    preempt. The endpoint appends a `checkpoint_complete` event to
    events.jsonl and makes no DB state changes."""
    submit = client.post(
        "/submit",
        json={"cmd": ["echo", "hi"], "cwd": "/tmp", "project": "project-a"},
    ).json()
    job_id = submit["id"]

    r = client.post(f"/jobs/{job_id}/checkpoint-complete")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}

    events = [e for e in _read_events(tmp_path) if e["event"] == "checkpoint_complete"]
    assert len(events) == 1, events
    assert events[0]["source"] == "broker"
    assert events[0]["job_id"] == job_id
    assert events[0]["project"] == "project-a"


def test_checkpoint_complete_endpoint_404_unknown_job(client):
    r = client.post("/jobs/9999999/checkpoint-complete")
    assert r.status_code == 404
    assert "no such job" in r.json()["detail"]


def test_refuse_admission_requeues_and_emits_event(client, tmp_path):
    """#41c: a worker that pulled a job and observed live GPU contention
    posts /refuse-admission. Broker reverts state QUEUED, clears worker +
    started_at, and writes an `admission_blocked` event."""
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
    submitted = client.post(
        "/submit",
        json={"cmd": ["echo", "hi"], "cwd": "/tmp", "project": "project-b"},
    ).json()
    job_id = submitted["id"]
    pulled = client.post(
        "/next-job",
        json={
            "host": "desktop",
            "free_vram_gb": 30.0,
            "unregistered_vram_gb": 0.0,
            "free_ram_gb": 28.0,
            "idle_cpus": 10,
        },
    ).json()
    assert pulled is not None
    assert pulled["state"] == "assigned"
    assert pulled["worker"] == "desktop"

    r = client.post(
        f"/jobs/{job_id}/refuse-admission",
        json={
            "required_gb": 8.0,
            "free_gb": 1.5,
            "foreign_pids": [4242, 4243],
            "foreign_vram_gb": 22.5,
        },
    )
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["state"] == "queued"
    assert info["worker"] is None
    assert info["started_at"] is None

    events = [e for e in _read_events(tmp_path) if e["event"] == "admission_blocked"]
    assert len(events) == 1, events
    e = events[0]
    assert e["source"] == "broker"
    assert e["job_id"] == job_id
    assert e["project"] == "project-b"
    p = e["payload"]
    assert p["worker"] == "desktop"
    assert p["required_gb"] == 8.0
    assert p["free_gb"] == 1.5
    assert p["foreign_pids"] == [4242, 4243]
    assert p["foreign_vram_gb"] == 22.5


def test_refuse_admission_409_when_not_assigned(client):
    """Job is queued (never pulled) → /refuse-admission returns 409."""
    submitted = client.post(
        "/submit",
        json={"cmd": ["echo", "hi"], "cwd": "/tmp", "project": "project-b"},
    ).json()
    job_id = submitted["id"]
    r = client.post(
        f"/jobs/{job_id}/refuse-admission",
        json={"required_gb": 8.0, "free_gb": 1.5},
    )
    assert r.status_code == 409
    assert "queued" in r.json()["detail"]
    assert "must be 'assigned'" in r.json()["detail"]


def test_refuse_admission_404_unknown_job(client):
    r = client.post(
        "/jobs/9999999/refuse-admission",
        json={"required_gb": 8.0, "free_gb": 1.5},
    )
    assert r.status_code == 404
    assert "no such job" in r.json()["detail"]


def test_submit_vram_gb_explicit_value_persists_to_job_row(client):
    """#41d end-to-end: `vram_gb` on JobSubmit lands on the Job row and
    surfaces through JobInfo. Without the field on JobSubmit, Pydantic
    silently dropped it and the matcher fell back to 0/tier-tag/floor."""
    info = client.post(
        "/submit",
        json={
            "cmd": ["echo", "ok"],
            "cwd": "/tmp",
            "project": "project-b",
            "vram_gb": 12.0,
        },
    ).json()
    assert info["vram_gb"] == 12.0


def test_submit_vram_gb_zero_uses_profile_default(client, tmp_path):
    """vram_gb omitted (default 0) — profile spec wins if one is selected."""
    info = client.post(
        "/submit",
        json={
            "cmd": ["echo", "ok"],
            "cwd": "/tmp",
            "project": "project-b",
        },
    ).json()
    assert info["vram_gb"] == 0.0


def _register_worker(client, host, mount_roots, host_aliases=None):
    """Register/heartbeat a worker advertising the given mount_roots."""
    return client.post(
        "/heartbeat",
        json={
            "host": host,
            "free_vram_gb": 20,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 30,
            "idle_cpus": 8,
            "host_aliases": host_aliases or [],
            "mount_roots": mount_roots,
        },
    )


def test_submit_hard_denies_pinned_cwd_without_mount_root(client):
    # Register a worker whose mount_roots do NOT cover the cwd, pinned to it.
    _register_worker(client, host="desktop", mount_roots=["/home", "/tmp"])
    r = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/mnt/d/data/x",
            "project": "project-a",
            "host_pin": "desktop",
        },
    )
    assert r.status_code == 400
    assert "/mnt/d/data/x" in r.text


def test_submit_hard_denies_any_pin_uncovered_cwd(client):
    # any-pin cwd under no worker's mount_roots is unroutable everywhere -> 400,
    # not a silent QUEUED sit (B never fires because no worker claims it).
    _register_worker(client, host="gt76", mount_roots=["/home", "/tmp"])
    r = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/scratch/run1",
            "project": "project-a",
            "host_pin": "any",
        },
    )
    assert r.status_code == 400
    assert "/scratch/run1" in r.text


def test_job_orm_has_excluded_workers_column():
    """B: the per-job cwd-refusal exclusion set column exists on the ORM."""
    from jobd.db import Job

    assert hasattr(Job, "excluded_workers_json")


def _submit_and_assign(client, cwd, worker, mount_roots=None, host_pin="any"):
    """Submit a job and claim it on `worker` via /next-job -> returns job id (ASSIGNED)."""
    mr = mount_roots if mount_roots is not None else ["/home"]
    r = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": cwd,
            "project": "project-a",
            "host_pin": host_pin,
        },
    )
    assert r.status_code == 200, r.text
    jid = r.json()["id"]
    got = client.post(
        "/next-job",
        json={
            "host": worker,
            "free_vram_gb": 30.0,
            "unregistered_vram_gb": 0.0,
            "free_ram_gb": 28.0,
            "idle_cpus": 10,
            "mount_roots": mr,
        },
    ).json()
    assert got is not None and got["id"] == jid, f"claim failed: {got}"
    return jid


def test_refuse_admission_cwd_missing_records_exclusion_and_requeues(client):
    _register_worker(client, host="laptop", mount_roots=["/home"])
    _register_worker(client, host="desktop", mount_roots=["/home"])
    cwd = "/home/u/p/.claude/worktrees/wt"
    jid = _submit_and_assign(client, cwd=cwd, worker="desktop")
    r = client.post(f"/jobs/{jid}/refuse-admission", json={"reason": "cwd_missing", "cwd": cwd})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "queued"  # laptop remains -> re-queued
    # desktop is excluded: a poll as desktop must not re-offer the job
    got = client.post(
        "/next-job",
        json={
            "host": "desktop",
            "free_vram_gb": 30.0,
            "unregistered_vram_gb": 0.0,
            "free_ram_gb": 28.0,
            "idle_cpus": 10,
            "mount_roots": ["/home"],
        },
    ).json()
    assert got is None or got["id"] != jid
    # laptop still gets it
    got2 = client.post(
        "/next-job",
        json={
            "host": "laptop",
            "free_vram_gb": 30.0,
            "unregistered_vram_gb": 0.0,
            "free_ram_gb": 28.0,
            "idle_cpus": 10,
            "mount_roots": ["/home"],
        },
    ).json()
    assert got2 is not None and got2["id"] == jid


def test_refuse_admission_cwd_missing_terminal_when_no_eligible_left(client):
    _register_worker(client, host="desktop", mount_roots=["/home"])
    cwd = "/home/u/p/wt"
    jid = _submit_and_assign(client, cwd=cwd, worker="desktop")
    r = client.post(f"/jobs/{jid}/refuse-admission", json={"reason": "cwd_missing", "cwd": cwd})
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["state"] == "failed"
    assert info["termination_reason"] == "cwd_unreachable"


def test_refuse_admission_cwd_unreachable_cascades_to_children(client):
    """audit 2026-07-01 H2: when a job fails cwd_unreachable (no eligible worker
    advertises its cwd), its default-policy dependents must cascade-cancel — the
    old path failed the parent without a cascade and stranded the child."""
    _register_worker(client, host="desktop", mount_roots=["/home"])
    cwd = "/home/u/p/wt3"
    parent_id = _submit_and_assign(client, cwd=cwd, worker="desktop")
    child = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/home/u/p/child",
            "project": "project-a",
            "depends_on": [parent_id],
        },
    ).json()

    r = client.post(
        f"/jobs/{parent_id}/refuse-admission", json={"reason": "cwd_missing", "cwd": cwd}
    )
    assert r.status_code == 200, r.text
    assert r.json()["termination_reason"] == "cwd_unreachable"

    row = client.get(f"/jobs/{child['id']}").json()
    assert row["state"] == "cancelled", row
    assert "parent_failed" in (row.get("warning") or "")


def test_refuse_admission_gpu_contention_unchanged(client):
    _register_worker(client, host="desktop", mount_roots=["/home"])
    jid = _submit_and_assign(client, cwd="/home/u/p", worker="desktop")
    r = client.post(f"/jobs/{jid}/refuse-admission", json={"required_gb": 20.0, "free_gb": 2.0})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "queued"


def test_next_job_skips_excluded_worker(client):
    _register_worker(client, host="laptop", mount_roots=["/home"])
    _register_worker(client, host="desktop", mount_roots=["/home"])
    cwd = "/home/u/p/wt2"
    jid = _submit_and_assign(client, cwd=cwd, worker="desktop")
    client.post(f"/jobs/{jid}/refuse-admission", json={"reason": "cwd_missing", "cwd": cwd})
    got = client.post(
        "/next-job",
        json={
            "host": "desktop",
            "free_vram_gb": 30.0,
            "unregistered_vram_gb": 0.0,
            "free_ram_gb": 28.0,
            "idle_cpus": 10,
            "mount_roots": ["/home"],
        },
    ).json()
    # Only one job exists and desktop is excluded -> desktop gets nothing.
    assert got is None
