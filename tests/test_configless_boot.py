"""A broker with NO config files must boot and run jobs (launch-prep dry-run).

The README has always promised "all three [config files] are optional; with
none present, every job runs at the global default priority" — but only
projects.yaml honored it. `pip install jobd && JOBD_ALLOW_NO_AUTH=1 jobd` on a
pristine machine crashed on the missing /app/config/profiles.yaml, meaning the
quickstart's very first command failed for every new user. CI never saw it
(exports JOBD_CONFIG_DIR to the repo's config/) and production never saw it
(Docker image ships /app/config). A clean-container dry-run did.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from jobd.app import build_app
from jobd.config import load_classifier_rules, load_profiles, load_projects


def test_all_three_loaders_tolerate_missing_files(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert load_profiles(missing / "profiles.yaml") == {}
    assert load_classifier_rules(missing / "classifier.yaml") == []
    projects = load_projects(missing / "projects.yaml")
    assert projects["_default"].priority == 40


def test_broker_boots_and_runs_a_job_with_no_config_at_all(tmp_path):
    """The quickstart contract, end to end: nonexistent config dir → app
    builds, /health answers, a submit lands QUEUED at the global default
    priority, and a worker can claim it."""
    ghost = tmp_path / "no-such-config-dir"
    app = build_app(
        db_url=f"sqlite:///{tmp_path}/jobd.db",
        projects_path=ghost / "projects.yaml",
        profiles_path=ghost / "profiles.yaml",
        classifier_path=ghost / "classifier.yaml",
        logs_path=tmp_path / "logs",
    )
    client = TestClient(app)

    assert client.get("/health").json()["status"] == "ok"

    r = client.post("/submit", json={"project": "anything", "cmd": ["echo", "hi"], "cwd": "/tmp"})
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["state"] == "queued"
    assert job["priority"] == 40  # the global default the README promises

    got = client.post(
        "/next-job",
        json={
            "host": "w1",
            "free_vram_gb": 0,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 8,
            "idle_cpus": 4,
        },
    ).json()
    assert got is not None and got["id"] == job["id"]


def test_worker_advertises_its_own_home_when_outside_candidates(tmp_path, monkeypatch):
    """A root-run worker's home is /root — outside the candidate roots — so a
    quickstart submit from $HOME got a mount-roots refusal (same dry-run)."""
    import jobd.worker.job_worker as jw

    monkeypatch.delenv("JOBD_WORKER_MOUNT_ROOTS", raising=False)
    real_isdir = jw.os.path.isdir
    monkeypatch.setattr(jw.os.path, "expanduser", lambda p: "/root")
    monkeypatch.setattr(jw.os.path, "isdir", lambda p: True if p == "/root" else real_isdir(p))
    roots = jw._detect_mount_roots()
    assert "/root" in roots


def test_worker_does_not_duplicate_a_home_already_covered(monkeypatch):
    """An ordinary /home/<user> is already covered by the /home candidate —
    no redundant entry."""
    import jobd.worker.job_worker as jw

    monkeypatch.delenv("JOBD_WORKER_MOUNT_ROOTS", raising=False)
    monkeypatch.setattr(jw.os.path, "expanduser", lambda p: "/home/someone")
    roots = jw._detect_mount_roots()
    assert "/home/someone" not in roots
    assert "/home" in roots


def test_mount_roots_override_still_wins(monkeypatch):
    import jobd.worker.job_worker as jw

    monkeypatch.setenv("JOBD_WORKER_MOUNT_ROOTS", "/data,/mnt/nas")
    assert jw._detect_mount_roots() == ["/data", "/mnt/nas"]
