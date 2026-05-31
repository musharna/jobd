"""End-to-end ETA wiring: history → JobInfo eta_* fields.

These tests exercise the whole stack via FastAPI TestClient: submit a
history of completed jobs (by directly inserting into the DB through the
broker session), then submit a fresh job and verify eta fields land on
the response.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from jobd.app import build_app
from jobd.db import Job
from jobd.models import JobState


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


def _seed_history(client, project, cmd, walls_s, max_wall_s=None):
    """Insert N completed jobs with deterministic walls into the broker DB."""
    SessionLocal = client.app.state.SessionLocal
    base = datetime(2026, 4, 1, tzinfo=UTC)
    with SessionLocal() as session:  # type: Session
        for i, w in enumerate(walls_s):
            started = base + timedelta(seconds=i * 100000)
            finished = started + timedelta(seconds=w)
            session.add(
                Job(
                    project=project,
                    host_pin="any",
                    priority=50,
                    state=JobState.COMPLETED.value,
                    cmd_json=json.dumps(cmd),
                    cwd="/tmp",
                    env_json="{}",
                    preemptible=False,
                    vram_gb=0,
                    ram_gb=0,
                    cpus=1,
                    submitted_at=started,
                    started_at=started,
                    finished_at=finished,
                    max_wall_s=max_wall_s,
                )
            )
        session.commit()


def test_submit_with_history_returns_eta_fields(client):
    cmd = ["python", "train.py"]
    _seed_history(client, "project-a", cmd, [100.0, 200.0, 300.0, 400.0, 500.0])

    r = client.post(
        "/submit",
        json={"cmd": cmd, "cwd": "/tmp", "project": "project-a"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "queued"
    assert body["eta_p50_s"] == pytest.approx(300.0)
    assert body["eta_p90_s"] == pytest.approx(460.0)
    assert body["eta_basis"] == "history-N=5"
    assert body["eta_clipped"] is False
    # No prior queue → start ETA zero (no competition).
    assert body["eta_start_p50_s"] == pytest.approx(0.0)


def test_submit_cold_start_reports_insufficient_history(client):
    r = client.post(
        "/submit",
        json={
            "cmd": ["python", "untouched.py"],
            "cwd": "/tmp",
            "project": "project-a",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["eta_basis"] == "insufficient-history-N=0"
    assert body["eta_p50_s"] is None
    assert body["eta_p90_s"] is None


def test_submit_ctest_with_cost_file_overrides_history_with_ctest_basis(
    client, tmp_path, monkeypatch
):
    """Part 2: when JOBD_CTEST_PARSE=1 and a CTestCostData.txt sits beside cwd,
    the broker reports ctest-cost-K basis with sum-of-costs, even though the
    history bucket would otherwise be ColdStart."""
    cost_dir = tmp_path / "build-wsl" / "Testing" / "Temporary"
    cost_dir.mkdir(parents=True)
    (cost_dir / "CTestCostData.txt").write_text(
        "FooTest.A 5 1.0\nFooTest.B 5 2.5\nBarTest.C 5 0.5\n---\n"
    )
    monkeypatch.setenv("JOBD_CTEST_PARSE", "1")

    r = client.post(
        "/submit",
        json={
            "cmd": ["ctest", "-R", "FooTest.*"],
            "cwd": str(tmp_path),
            "project": "project-a",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["eta_basis"] == "ctest-cost-K=2"
    assert body["eta_p50_s"] == pytest.approx(3.5)
    assert body["eta_p90_s"] == pytest.approx(3.5)


def test_submit_ctest_without_env_falls_through_to_history(client, tmp_path, monkeypatch):
    """JOBD_CTEST_PARSE unset → ctest path is dormant, normal ColdStart applies."""
    cost_dir = tmp_path / "build-wsl" / "Testing" / "Temporary"
    cost_dir.mkdir(parents=True)
    (cost_dir / "CTestCostData.txt").write_text("FooTest.A 5 1.0\n---\n")
    monkeypatch.delenv("JOBD_CTEST_PARSE", raising=False)

    r = client.post(
        "/submit",
        json={
            "cmd": ["ctest", "-R", "FooTest.*"],
            "cwd": str(tmp_path),
            "project": "project-a",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["eta_basis"] == "insufficient-history-N=0"


def test_submit_ctest_no_match_falls_through_to_history(client, tmp_path, monkeypatch):
    """Regex matches nothing in cost file → fall through to history basis."""
    cost_dir = tmp_path / "build-wsl" / "Testing" / "Temporary"
    cost_dir.mkdir(parents=True)
    (cost_dir / "CTestCostData.txt").write_text("FooTest.A 5 1.0\n---\n")
    monkeypatch.setenv("JOBD_CTEST_PARSE", "1")

    r = client.post(
        "/submit",
        json={
            "cmd": ["ctest", "-R", "NoSuchTest"],
            "cwd": str(tmp_path),
            "project": "project-a",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["eta_basis"] == "insufficient-history-N=0"


def test_terminal_jobs_have_no_eta_fields(client):
    cmd = ["python", "x.py"]
    _seed_history(client, "project-a", cmd, [100.0] * 5)
    # Pull the most recent completed seeded job via the list endpoint.
    r = client.get("/jobs", params={"project": "project-a"})
    assert r.status_code == 200
    completed = [j for j in r.json() if j["state"] == "completed"]
    assert completed
    for j in completed:
        assert j["eta_p50_s"] is None
        assert j["eta_basis"] is None
        assert j["eta_clipped"] is False


def test_running_job_has_remaining_eta(client):
    cmd = ["python", "train.py"]
    _seed_history(client, "project-a", cmd, [600.0] * 5)
    SessionLocal = client.app.state.SessionLocal
    with SessionLocal() as session:
        # Anchor to actual wall-clock so the broker's datetime.now(UTC) sees
        # the seeded started_at as ~200s in the past, not 2026-04-28 in the
        # past (which would clamp remaining to 0).
        now = datetime.now(UTC)
        # Insert a running job that started 200s ago.
        job = Job(
            project="project-a",
            host_pin="any",
            priority=50,
            state=JobState.RUNNING.value,
            cmd_json=json.dumps(cmd),
            cwd="/tmp",
            env_json="{}",
            preemptible=False,
            vram_gb=0,
            ram_gb=0,
            cpus=1,
            submitted_at=now - timedelta(seconds=200),
            started_at=now - timedelta(seconds=200),
        )
        session.add(job)
        session.commit()
        running_id = job.id

    r = client.get(f"/jobs/{running_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "running"
    assert body["eta_p50_s"] == pytest.approx(600.0)
    # Remaining = 600 - elapsed (~now-started, around the test's now()).
    # We can't pin elapsed exactly because the broker computes "now" itself,
    # so just bound it.
    assert body["eta_remaining_p50_s"] is not None
    assert 0 <= body["eta_remaining_p50_s"] <= 600
    assert body["eta_start_p50_s"] is None  # only queued jobs get start ETA


def test_clipping_flag_propagates(client):
    cmd = ["python", "train.py"]
    # 3 short, 2 clipped (within 95% of max_wall_s=100, i.e. wall=98)
    SessionLocal = client.app.state.SessionLocal
    base = datetime(2026, 4, 1, tzinfo=UTC)
    with SessionLocal() as session:
        for i, (w, mw) in enumerate(
            [(10.0, None), (12.0, None), (15.0, None), (98.0, 100), (99.0, 100)]
        ):
            started = base + timedelta(seconds=i * 1000)
            finished = started + timedelta(seconds=w)
            session.add(
                Job(
                    project="project-a",
                    host_pin="any",
                    priority=50,
                    state=JobState.COMPLETED.value,
                    cmd_json=json.dumps(cmd),
                    cwd="/tmp",
                    env_json="{}",
                    submitted_at=started,
                    started_at=started,
                    finished_at=finished,
                    max_wall_s=mw,
                )
            )
        session.commit()

    r = client.post(
        "/submit",
        json={"cmd": cmd, "cwd": "/tmp", "project": "project-a"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["eta_clipped"] is True


def test_queue_eta_aggregates_running_and_ahead_queue(client):
    """One running + one ahead-queued, shared 'any' pool → eta_start sums them."""
    cmd = ["python", "t.py"]
    _seed_history(client, "project-a", cmd, [600.0] * 5)
    SessionLocal = client.app.state.SessionLocal
    # The fresh submit lands at project-a's priority (55 in fixture). Seed
    # competing rows at the same priority so they compete with the target.
    target_priority = 55
    with SessionLocal() as session:
        # Anchor to wall-clock so 100s-elapsed math holds against broker's now.
        now = datetime.now(UTC)
        # Running, 100s elapsed → 500s remaining (p50=600).
        session.add(
            Job(
                project="project-a",
                host_pin="any",
                priority=target_priority,
                state=JobState.RUNNING.value,
                cmd_json=json.dumps(cmd),
                cwd="/tmp",
                env_json="{}",
                submitted_at=now - timedelta(seconds=100),
                started_at=now - timedelta(seconds=100),
            )
        )
        # Ahead-queued at same priority but earlier id.
        session.add(
            Job(
                project="project-a",
                host_pin="any",
                priority=target_priority,
                state=JobState.QUEUED.value,
                cmd_json=json.dumps(cmd),
                cwd="/tmp",
                env_json="{}",
                submitted_at=now,
            )
        )
        session.commit()

    r = client.post(
        "/submit",
        json={"cmd": cmd, "cwd": "/tmp", "project": "project-a"},
    )
    assert r.status_code == 200
    body = r.json()
    # Running's remaining ~ <=500 + ahead-queued's p50=600.
    # Allow elapsed slop in the "remaining" arithmetic.
    assert body["eta_start_p50_s"] is not None
    assert 1000 <= body["eta_start_p50_s"] <= 1100
