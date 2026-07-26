"""A broker with NO config files must boot and run jobs (launch-prep dry-run).

The README has always promised "all three [config files] are optional; with
none present, every job runs at the global default priority" — but only
projects.yaml honored it. `pip install jobd && JOBD_ALLOW_NO_AUTH=1 jobd` on a
pristine machine crashed on the missing /app/config/profiles.yaml, meaning the
quickstart's very first command failed for every new user. CI never saw it
(exports JOBD_CONFIG_DIR to the repo's config/) and production never saw it
(Docker image ships /app/config). A clean-container dry-run did.

2026-07-26: the SAME dry-run, re-run against PyPI 0.5.34 from a bare
`pip install` rather than the Docker image, found the next step of the very
same command still broken. `JOBD_DB_URL` defaulted to `sqlite:////app/data/
jobd.db` — the *container's* path — so the broker died on boot with SQLite's
opaque "unable to open database file" on any machine that was not the image.
That default had been there since the first commit and had never worked for a
pip install on any released version.

Three layers hid it, and all three are the same mistake: **the check supplied
the value the user does not have.** The Dockerfile sets JOBD_DB_URL as an
explicit ENV and mkdirs /app/data. CI sets it. And
`test_broker_boots_and_runs_a_job_with_no_config_at_all` below passes
`db_url=` directly to build_app, so it exercises everything about a configless
boot except the configless part of the DB path. The tests added at the bottom
of this file go through `main.run()` with no environment at all, which is the
only way this class of bug is visible.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jobd.app import build_app
from jobd.config import load_classifier_rules, load_profiles, load_projects
from jobd.main import default_db_url, ensure_sqlite_parent


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


# --- the database path a bare `pip install jobd` gets (2026-07-26 dry-run) ---


def test_default_db_url_is_not_a_container_path():
    """The regression itself: the default must be usable off the Docker image."""
    url = default_db_url()
    assert "/app/data" not in url, (
        "the default DB path is the container's own; a pip install has no /app "
        "and SQLite will not create it"
    )
    assert url.startswith("sqlite:///")


def test_default_db_url_honours_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert default_db_url() == f"sqlite:///{tmp_path / 'xdg' / 'jobd' / 'jobd.db'}"


def test_default_db_url_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    expected = tmp_path / ".local" / "share" / "jobd" / "jobd.db"
    assert default_db_url() == f"sqlite:///{expected}"


def test_ensure_sqlite_parent_creates_missing_directories(tmp_path):
    target = tmp_path / "deep" / "nested" / "jobd.db"
    assert not target.parent.exists()
    ensure_sqlite_parent(f"sqlite:///{target}")
    assert target.parent.is_dir()


def test_ensure_sqlite_parent_tolerates_query_params(tmp_path):
    target = tmp_path / "q" / "jobd.db"
    ensure_sqlite_parent(f"sqlite:///{target}?check_same_thread=false")
    assert target.parent.is_dir()


@pytest.mark.parametrize("url", ["postgresql://host/db", "sqlite://", "sqlite:///:memory:"])
def test_ensure_sqlite_parent_ignores_urls_with_no_local_directory(url):
    ensure_sqlite_parent(url)  # must not raise, must not create anything


def test_ensure_sqlite_parent_fails_loudly_when_it_cannot_create(tmp_path):
    """SQLite's own error names neither the path nor the fix. Ours must."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file")
    with pytest.raises(SystemExit) as exc:
        ensure_sqlite_parent(f"sqlite:///{blocker}/sub/jobd.db")
    msg = str(exc.value)
    assert "JOBD_DB_URL" in msg, "the error must name the escape hatch"
    assert str(blocker) in msg, "the error must name the path that failed"


def test_run_boots_with_a_completely_empty_environment(monkeypatch, tmp_path):
    """The end-to-end contract, through `main.run()` rather than around it.

    This is the test whose absence let the bug ship: everything else supplied
    `db_url` itself. Here the only inputs are an empty environment and a home
    directory, exactly as on a stranger's machine after `pip install jobd`.
    """
    import jobd.main as main

    for var in ("JOBD_DB_URL", "XDG_DATA_HOME", "JOBD_CONFIG_DIR", "JOBD_LOGS_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("JOBD_ALLOW_NO_AUTH", "1")
    monkeypatch.setattr("sys.argv", ["jobd"])

    captured: dict = {}
    monkeypatch.setattr(main, "build_app", lambda **kw: captured.update(kw) or object())
    monkeypatch.setattr(main.uvicorn, "run", lambda *a, **k: captured.update(served=True))

    main.run()

    assert captured.get("served") is True, "broker never reached uvicorn.run"
    db_url = captured["db_url"]
    assert "/app/data" not in db_url
    db_file = db_url[len("sqlite:///") :]
    assert db_file.startswith(str(tmp_path)), f"DB escaped the fake home: {db_file}"
    from pathlib import Path

    assert Path(db_file).parent.is_dir(), "boot must create the directory SQLite needs"


def test_explicit_jobd_db_url_still_wins(monkeypatch, tmp_path):
    """The Docker image's contract. The Dockerfile sets JOBD_DB_URL=/app/data
    as an ENV and mkdirs the directory, so changing the *code* default must not
    move the database out from under an existing container deployment."""
    import jobd.main as main

    chosen = tmp_path / "explicit" / "somewhere.db"
    monkeypatch.setenv("JOBD_DB_URL", f"sqlite:///{chosen}")
    monkeypatch.setenv("HOME", str(tmp_path / "unused-home"))
    monkeypatch.setenv("JOBD_ALLOW_NO_AUTH", "1")
    monkeypatch.setattr("sys.argv", ["jobd"])

    captured: dict = {}
    monkeypatch.setattr(main, "build_app", lambda **kw: captured.update(kw) or object())
    monkeypatch.setattr(main.uvicorn, "run", lambda *a, **k: None)

    main.run()

    assert captured["db_url"] == f"sqlite:///{chosen}"
    assert chosen.parent.is_dir(), "an explicit path should get its directory created too"
