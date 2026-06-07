"""Tests for jobd database schema."""

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from jobd.db import Job, Worker, init_db, migrate


def test_init_db_creates_tables(tmp_db_url):
    engine = create_engine(tmp_db_url)
    init_db(engine)
    with engine.connect() as conn:
        tables = set(
            row[0]
            for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
        )
    assert {"jobs", "workers", "bypass_log"}.issubset(tables)


def test_job_round_trip(tmp_db_url):
    from datetime import UTC, datetime

    engine = create_engine(tmp_db_url)
    init_db(engine)
    with Session(engine) as session:
        job = Job(
            project="project-a",
            profile="gpu-heavy",
            host_pin="desktop",
            priority=65,
            state="queued",
            cmd_json='["bash", "train.sh"]',
            cwd="/tmp",
            preemptible=True,
            submitted_at=datetime.now(UTC),
        )
        session.add(job)
        session.commit()
        assert job.id is not None

        got = session.execute(select(Job).where(Job.id == job.id)).scalar_one()
        assert got.project == "project-a"
        assert got.priority == 65


def test_worker_unique_host(tmp_db_url):
    from datetime import UTC, datetime

    import pytest
    from sqlalchemy.exc import IntegrityError

    engine = create_engine(tmp_db_url)
    init_db(engine)
    with Session(engine) as session:
        session.add(Worker(host="desktop", last_heartbeat=datetime.now(UTC)))
        session.commit()
        session.add(Worker(host="desktop", last_heartbeat=datetime.now(UTC)))
        with pytest.raises(IntegrityError):
            session.commit()


def test_migrate_adds_new_columns_if_missing(tmp_db_url):
    engine = create_engine(tmp_db_url)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY, project TEXT, "
            "profile TEXT, host_pin TEXT, priority INTEGER, state TEXT, "
            "cmd_json TEXT, cwd TEXT, env_json TEXT DEFAULT '{}', "
            "preemptible BOOLEAN DEFAULT 0, vram_gb REAL DEFAULT 0, "
            "ram_gb REAL DEFAULT 0, cpus INTEGER DEFAULT 1, worker TEXT, "
            "session_id TEXT, submitted_at DATETIME, started_at DATETIME, "
            "finished_at DATETIME, exit_code INTEGER, signal TEXT)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE workers (id INTEGER PRIMARY KEY, host TEXT UNIQUE, "
            "host_aliases_json TEXT DEFAULT '[]', last_heartbeat DATETIME, "
            "free_vram_gb REAL DEFAULT 0, unregistered_vram_gb REAL DEFAULT 0, "
            "free_ram_gb REAL DEFAULT 0, idle_cpus INTEGER DEFAULT 0)"
        )
    migrate(engine)
    insp = inspect(engine)
    job_cols = {c["name"] for c in insp.get_columns("jobs")}
    worker_cols = {c["name"] for c in insp.get_columns("workers")}
    assert {"requires_json", "warning", "warning_at"}.issubset(job_cols)
    assert {"arch", "os", "gpu", "tags_json", "state"}.issubset(worker_cols)

    migrate(engine)


def test_migrate_on_fresh_db(tmp_db_url):
    engine = create_engine(tmp_db_url)
    init_db(engine)
    migrate(engine)
    insp = inspect(engine)
    job_cols = {c["name"] for c in insp.get_columns("jobs")}
    assert "requires_json" in job_cols
