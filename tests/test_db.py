"""Tests for jobd database schema."""
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from jobd.db import Base, Job, Worker, BypassLog, init_db


def test_init_db_creates_tables(tmp_db_url):
    engine = create_engine(tmp_db_url)
    init_db(engine)
    with engine.connect() as conn:
        tables = set(
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        )
    assert {"jobs", "workers", "bypass_log"}.issubset(tables)


def test_job_round_trip(tmp_db_url):
    from datetime import datetime, UTC

    engine = create_engine(tmp_db_url)
    init_db(engine)
    with Session(engine) as session:
        job = Job(
            project="orchid-sdxl",
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
        assert got.project == "orchid-sdxl"
        assert got.priority == 65


def test_worker_unique_host(tmp_db_url):
    from datetime import datetime, UTC
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
