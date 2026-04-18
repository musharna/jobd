"""SQLAlchemy models for jobd persistence."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
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
