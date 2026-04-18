"""Pydantic request/response models for the jobd HTTP API."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


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


class WorkerHeartbeat(BaseModel):
    host: str
    free_vram_gb: float
    unregistered_vram_gb: float
    free_ram_gb: float
    idle_cpus: int
    host_aliases: list[str] = Field(default_factory=list)


class NextJobQuery(BaseModel):
    host: str
    free_vram_gb: float
    unregistered_vram_gb: float
    free_ram_gb: float
    idle_cpus: int


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
    requires: list[str] = Field(default_factory=list)
