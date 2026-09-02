"""The label column, including the migration path for a live DB.

NULL means "same as project". That choice is what makes this a pure ALTER with
no backfill: every one of the 3,608 existing rows is already correct under it.
"""

from sqlalchemy import create_engine, inspect, text

from jobd.db import Base, migrate


def _drop_project_label(engine) -> None:
    """Turn a fresh schema into the pre-column one. SQLite refuses to drop a
    column an index references, so the index goes first."""
    with engine.begin() as conn:
        for idx in inspect(engine).get_indexes("jobs"):
            if idx["column_names"] == ["project_label"]:
                conn.execute(text(f"DROP INDEX {idx['name']}"))
        conn.execute(text("ALTER TABLE jobs DROP COLUMN project_label"))


def test_migrate_adds_project_label_to_a_pre_existing_table(tmp_path):
    """The real-execution check for the migration: build a jobs table WITHOUT
    the column, run migrate, and read the live schema back. A test that only
    creates a fresh table via metadata would never exercise the ALTER."""
    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    _drop_project_label(engine)
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


def test_project_label_is_indexed_on_a_fresh_table(tmp_path):
    """audit 2026-09-02 L-5: `job list --project` ORs on project_label, so an
    unindexed column turns every filtered list into a table scan."""
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    Base.metadata.create_all(engine)
    indexed = {tuple(i["column_names"]) for i in inspect(engine).get_indexes("jobs")}
    assert ("project_label",) in indexed


def test_migrate_adds_the_project_label_index_to_a_pre_existing_table(tmp_path):
    """An in-place upgrade gets the ALTER for the column; it must get the
    index too, or only fresh databases are fast."""
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    Base.metadata.create_all(engine)
    _drop_project_label(engine)
    migrate(engine)
    indexed = {tuple(i["column_names"]) for i in inspect(engine).get_indexes("jobs")}
    assert ("project_label",) in indexed
    migrate(engine)  # idempotent: no "index already exists"
