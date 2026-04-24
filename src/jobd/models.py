"""Pydantic request/response models for the jobd HTTP API."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


def _as_utc_iso(v: datetime | None) -> str | None:
    """Serialize a datetime as ISO-8601 with explicit UTC offset.

    jobd stores timestamps via datetime.now(UTC) but the SQLite column is
    plain DateTime, so tzinfo is stripped on persistence and values come
    back naive. Treat naive reads as UTC (which they are) so API consumers
    get unambiguous offsets instead of a bare local-looking timestamp.
    """
    if v is None:
        return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=UTC)
    return v.isoformat()


class JobState(StrEnum):
    QUEUED = "queued"
    ASSIGNED = "assigned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PREEMPTED = "preempted"
    PREEMPT_REQUESTED = "preempt-requested"
    ORPHANED = "orphaned"


class ResourceReq(BaseModel):
    vram_gb: float = 0
    ram_gb: float = 0
    cpus: int = 1


class JobRequires(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arch: str = "any"
    os: str = "any"
    gpu: bool | None = None
    needs: list[str] = Field(default_factory=list)
    idempotent: bool = False


class JobSubmit(BaseModel):
    cmd: list[str] = Field(..., min_length=1)
    cwd: str
    project: str
    profile: str | None = None
    host_pin: str = "any"
    priority_delta: int = 0
    preemptible: bool = False
    env: dict[str, str] = Field(default_factory=dict)
    session_id: str | None = None
    requires: JobRequires | None = None

    @field_validator("priority_delta")
    @classmethod
    def clamp_delta(cls, v: int) -> int:
        return max(-50, min(50, v))


class JobInfo(BaseModel):
    id: int
    project: str
    profile: str | None
    host_pin: str
    priority: int
    state: JobState
    cmd: list[str]
    cwd: str
    preemptible: bool
    worker: str | None
    submitted_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    exit_code: int | None
    vram_gb: float = 0
    ram_gb: float = 0
    cpus: int = 1
    requires: JobRequires | None = None
    warning: str | None = None

    @field_serializer("submitted_at", "started_at", "finished_at", when_used="always")
    def _ser_dt(self, v: datetime | None) -> str | None:
        return _as_utc_iso(v)


class WorkerInfo(BaseModel):
    host: str
    host_aliases: list[str]
    last_heartbeat: datetime
    state: str
    free_vram_gb: float
    free_ram_gb: float
    idle_cpus: int
    arch: str
    os: str
    gpu: bool
    tags: list[str]

    @field_serializer("last_heartbeat", when_used="always")
    def _ser_dt(self, v: datetime) -> str | None:
        return _as_utc_iso(v)


class WorkerHeartbeat(BaseModel):
    host: str
    free_vram_gb: float
    unregistered_vram_gb: float
    free_ram_gb: float
    idle_cpus: int
    host_aliases: list[str] = Field(default_factory=list)
    arch: str = "unknown"
    os: str = "unknown"
    gpu: bool = False
    tags: list[str] = Field(default_factory=list)


class NextJobQuery(BaseModel):
    host: str
    free_vram_gb: float
    unregistered_vram_gb: float
    free_ram_gb: float
    idle_cpus: int
    arch: str = "unknown"
    os: str = "unknown"
    gpu: bool = False
    tags: list[str] = Field(default_factory=list)


class ClassifyRequest(BaseModel):
    cmd: str


class ClassifyResult(BaseModel):
    heavy: bool
    rule_id: str | None = None
    suggest_profile: str | None = None
    confidence: Literal["high", "medium", "low"] | None = None
    reason: str | None = None


class ProjectPriority(BaseModel):
    name: str
    priority: int = Field(ge=0, le=100)


class ProfileSpec(BaseModel):
    name: str
    vram_gb: float = 0
    ram_gb: float = 0
    cpus: int = 1
    expected_runtime: str = "1h"
    preemptible: bool = False
    host_hint: str = "any"
    exclusive: bool = False
    fast_path: bool = False
    requires: JobRequires | None = None
