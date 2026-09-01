"""The label column, including the migration path for a live DB.

NULL means "same as project". That choice is what makes this a pure ALTER with
no backfill: every one of the 3,608 existing rows is already correct under it.
"""

from sqlalchemy import create_engine, inspect, text

from jobd.db import Base, migrate


def test_migrate_adds_project_label_to_a_pre_existing_table(tmp_path):
    """The real-execution check for the migration: build a jobs table WITHOUT
    the column, run migrate, and read the live schema back. A test that only
    creates a fresh table via metadata would never exercise the ALTER."""
    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE jobs DROP COLUMN project_label"))
    assert "project_label" not in {c["name"] for c in inspect(engine).get_columns("jobs")}

    migrate(engine)

    cols = {c["name"] for c in inspect(engine).get_columns("jobs")}
    assert "project_label" in cols


def test_migrate_is_idempotent(tmp_path):
    """Positive control: migrate runs on every broker start, so a second pass
    must not raise 'duplicate column name'."""
    url = f"sqlite:///{tmp_path / 'x.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    migrate(engine)
    migrate(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("jobs")}
    assert "project_label" in cols
