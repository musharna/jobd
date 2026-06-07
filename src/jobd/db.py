"""SQLAlchemy models for jobd persistence."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    inspect as _inspect,
)
from sqlalchemy import (
    text as _text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project: Mapped[str] = mapped_column(String(100), index=True)
    profile: Mapped[str | None] = mapped_column(String(100), nullable=True)
    host_pin: Mapped[str] = mapped_column(String(50), default="any")
    priority: Mapped[int] = mapped_column(Integer, index=True)
    state: Mapped[str] = mapped_column(String(30), index=True)
    cmd_json: Mapped[str] = mapped_column(Text)
    cwd: Mapped[str] = mapped_column(Text)
    env_json: Mapped[str] = mapped_column(Text, default="{}")
    preemptible: Mapped[bool] = mapped_column(Boolean, default=False)
    vram_gb: Mapped[float] = mapped_column(Float, default=0.0)
    ram_gb: Mapped[float] = mapped_column(Float, default=0.0)
    cpus: Mapped[int] = mapped_column(Integer, default=1)
    worker: Mapped[str | None] = mapped_column(String(50), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    signal: Mapped[str | None] = mapped_column(String(30), nullable=True)
    requires_json: Mapped[str] = mapped_column(Text, default="{}")
    warning: Mapped[str | None] = mapped_column(Text, nullable=True)
    warning_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    depends_on_json: Mapped[str] = mapped_column(Text, default="[]")
    depends_on_any_exit: Mapped[bool] = mapped_column(Boolean, default=False)
    fast_path: Mapped[bool] = mapped_column(Boolean, default=False)
    max_wall_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idle_timeout_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checkpoint_grace_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Audit 2026-05-18 (runtime-zombies S3): see JobSubmit.scheduling_timeout_s.
    scheduling_timeout_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    termination_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    submitted_via: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Phase 3 (job arrays): all NULL for a standalone job. A member is a normal
    # job plus these grouping columns; array_id is the first member's job id, so
    # `job status A<id>` / `job list --array A<id>` resolve without a new id
    # sequence. array_index is 0-based; array_size is the member count.
    array_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    array_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    array_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    @property
    def requires(self):
        """Parse requires_json to JobRequires | None for matcher Protocol.

        Malformed JSON is treated as null so a corrupt row can't crash the
        matcher loop; callers that need validation should use JobRequires
        directly on submit.
        """
        from pydantic import ValidationError

        from jobd.models import JobRequires

        if not self.requires_json or self.requires_json == "{}":
            return None
        try:
            return JobRequires.model_validate_json(self.requires_json)
        except (ValidationError, ValueError):
            return None


class Worker(Base):
    __tablename__ = "workers"
    __table_args__ = (UniqueConstraint("host", name="uq_worker_host"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host: Mapped[str] = mapped_column(String(50))
    host_aliases_json: Mapped[str] = mapped_column(Text, default="[]")
    last_heartbeat: Mapped[datetime] = mapped_column(DateTime, index=True)
    free_vram_gb: Mapped[float] = mapped_column(Float, default=0.0)
    unregistered_vram_gb: Mapped[float] = mapped_column(Float, default=0.0)
    free_ram_gb: Mapped[float] = mapped_column(Float, default=0.0)
    idle_cpus: Mapped[int] = mapped_column(Integer, default=0)
    arch: Mapped[str] = mapped_column(String(30), default="unknown")
    os: Mapped[str] = mapped_column(String(30), default="unknown")
    gpu: Mapped[bool] = mapped_column(Boolean, default=False)
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    state: Mapped[str] = mapped_column(String(20), default="online", index=True)
    mount_roots_json: Mapped[str] = mapped_column(Text, default="[]")
    # Concurrency/multislotting observability: max_concurrent is the worker's
    # JOBD_WORKER_MAX_CONCURRENT_JOBS; running is its live in-flight job count.
    # Surfaced as `running/max` slots in `job workers`. Default 1/0 so an old
    # worker (pre-field heartbeat) reads as single-slot, idle.
    max_concurrent: Mapped[int] = mapped_column(Integer, default=1)
    running: Mapped[int] = mapped_column(Integer, default=0)


class BypassLog(Base):
    __tablename__ = "bypass_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    session_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    project: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cmd: Mapped[str] = mapped_column(Text)
    cwd: Mapped[str | None] = mapped_column(Text, nullable=True)
    host: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


def init_db(engine) -> None:
    """Create all tables if they do not exist."""
    Base.metadata.create_all(engine)


_JOB_ADDS = [
    ("requires_json", "TEXT DEFAULT '{}'"),
    ("warning", "TEXT"),
    ("warning_at", "DATETIME"),
    ("depends_on_json", "TEXT DEFAULT '[]'"),
    ("depends_on_any_exit", "BOOLEAN DEFAULT 0"),
    ("fast_path", "BOOLEAN DEFAULT 0"),
    ("max_wall_s", "INTEGER"),
    ("idle_timeout_s", "INTEGER"),
    ("checkpoint_grace_s", "INTEGER"),
    ("termination_reason", "VARCHAR(50)"),
    ("submitted_via", "VARCHAR(10)"),
    ("scheduling_timeout_s", "INTEGER"),
    ("array_id", "INTEGER"),
    ("array_index", "INTEGER"),
    ("array_size", "INTEGER"),
]
_WORKER_ADDS = [
    ("arch", "VARCHAR(30) DEFAULT 'unknown'"),
    ("os", "VARCHAR(30) DEFAULT 'unknown'"),
    ("gpu", "BOOLEAN DEFAULT 0"),
    ("tags_json", "TEXT DEFAULT '[]'"),
    ("state", "VARCHAR(20) DEFAULT 'online'"),
    ("mount_roots_json", "TEXT DEFAULT '[]'"),
    ("max_concurrent", "INTEGER DEFAULT 1"),
    ("running", "INTEGER DEFAULT 0"),
]


def migrate(engine) -> None:
    """Additive SQLite migration for Phase 2 capability columns. Idempotent."""
    insp = _inspect(engine)
    if "jobs" in insp.get_table_names():
        existing = {c["name"] for c in insp.get_columns("jobs")}
        with engine.begin() as conn:
            for col, ddl in _JOB_ADDS:
                if col not in existing:
                    conn.execute(_text(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}"))
    if "workers" in insp.get_table_names():
        existing = {c["name"] for c in insp.get_columns("workers")}
        with engine.begin() as conn:
            for col, ddl in _WORKER_ADDS:
                if col not in existing:
                    conn.execute(_text(f"ALTER TABLE workers ADD COLUMN {col} {ddl}"))
