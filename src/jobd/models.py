"""Pydantic request/response models for the jobd HTTP API."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

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
    # Audit 2026-05-18 (runtime-zombies S3): terminal state for jobs whose
    # caller-supplied scheduling_timeout_s elapsed while still queued. Pattern
    # borrowed from Hatchet's scheduling_timeout; jobs 577/578 on server motivated
    # this — capability-mismatch echoes that would never dispatch and had no
    # DLQ. Only fires when scheduling_timeout_s is explicitly set.
    SCHEDULING_TIMEOUT = "scheduling_timeout"


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
    # `None` = caller didn't pass --preemptible, defer to project defaults /
    # profile / global. Explicit `True`/`False` overrides those.
    preemptible: bool | None = None
    env: dict[str, str] = Field(default_factory=dict)
    session_id: str | None = None
    requires: JobRequires | None = None
    depends_on: list[int] = Field(default_factory=list)
    depends_on_any_exit: bool = False
    fast_path: bool | None = None
    # Explicit GPU VRAM the job needs at dispatch (GB). Resolution order at
    # the broker: this field > profile_spec.vram_gb > 0. Matcher then
    # falls through to cuda-Ngb tier tags in `requires.needs` and the
    # 2 GB GPU_IMPLICIT_FLOOR_GB if still 0 with `requires.gpu==True`.
    vram_gb: float = Field(default=0, ge=0)
    max_wall_s: int | None = Field(default=None, ge=1, le=7 * 24 * 3600)
    idle_timeout_s: int | None = Field(default=None, ge=1, le=24 * 3600)
    # Audit 2026-05-18 (runtime-zombies S3): cap how long a job can sit queued
    # before the broker auto-terminates it as scheduling_timeout. `None`
    # (the default) = opt-out: a job waits indefinitely for a capable worker,
    # mirroring the `host_pin`/`preemptible` "None = defer" convention. This is
    # deliberate — a default 300s timeout silently kills legitimate jobs that
    # queue behind long-running work or wait on a momentarily-saturated GPU
    # (the sweeper keys on broker QUEUED state, which such jobs sit in). Callers
    # that genuinely want a stuck-queue guard opt in via an explicit value;
    # bounds match max_wall_s so an opted-in timeout can't exceed 1 week.
    scheduling_timeout_s: int | None = Field(default=None, ge=1, le=7 * 24 * 3600)
    # Per-job preemption grace window (seconds). When the worker observes
    # signal=preempt, it SIGTERMs the child and waits up to this many seconds
    # for the workload to checkpoint and exit cleanly before SIGKILL. Capped
    # at 300s (sweeper interval) so a stuck checkpoint can't pin a slot.
    # None = use the worker's hard-coded 60s default.
    checkpoint_grace_s: int | None = Field(default=None, ge=1, le=300)
    # Submission origin marker. CLI sets "cli", jobd-mcp sets "mcp". Used to
    # answer "are sessions actually using the MCP?" — observable via SQL
    # `SELECT submitted_via, COUNT(*) FROM jobs GROUP BY submitted_via`.
    # session_id is unreliable here: the MCP protocol exposes no Claude
    # session id to the tool process, so it stays None for organic chat use.
    submitted_via: Literal["cli", "mcp"] | None = None
    # Preview mode.
    # When True, /submit runs the full validation + routing-decision path
    # (profile lookup, project defaults, cwd sanity, depends_on existence,
    # preflight, gpu_contention) and returns the would-be plan WITHOUT
    # inserting a Job row or emitting `job_submitted`. Defaults to False to
    # keep the live path ergonomic — confirming every submit is unusable.
    dry_run: bool = False

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
    # Caller-submitted environment variables, forwarded to the worker so the
    # workload subprocess actually receives them (the worker layers these over
    # its inherited env; jobd-internal JOBD_* vars take precedence). Trusted-
    # tailnet feature — see docs/security.md.
    env: dict[str, str] = Field(default_factory=dict)
    requires: JobRequires | None = None
    warning: str | None = None
    depends_on: list[int] = Field(default_factory=list)
    depends_on_any_exit: bool = False
    session_id: str | None = None
    fast_path: bool = False
    max_wall_s: int | None = None
    idle_timeout_s: int | None = None
    checkpoint_grace_s: int | None = None
    scheduling_timeout_s: int | None = None
    termination_reason: str | None = None
    # Time-estimation fields (None for terminal jobs and when no history bucket).
    # Always quoted in seconds; CLI formats. eta_basis identifies the source
    # so callers can distinguish "history-N=12" from "insufficient-history-N=2".
    eta_p50_s: float | None = None
    eta_p90_s: float | None = None
    eta_remaining_p50_s: float | None = None
    eta_remaining_p90_s: float | None = None
    eta_start_p50_s: float | None = None
    eta_basis: str | None = None
    eta_clipped: bool = False
    submitted_via: Literal["cli", "mcp"] | None = None

    @field_serializer("submitted_at", "started_at", "finished_at", when_used="always")
    def _ser_dt(self, v: datetime | None) -> str | None:
        return _as_utc_iso(v)


class WorkerInfo(BaseModel):
    host: str
    host_aliases: list[str]
    last_heartbeat: datetime
    state: str
    free_vram_gb: float
    unregistered_vram_gb: float = 0.0
    free_ram_gb: float
    idle_cpus: int
    arch: str
    os: str
    gpu: bool
    tags: list[str]
    mount_roots: list[str] = Field(default_factory=list)

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
    mount_roots: list[str] = Field(default_factory=list)


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
    mount_roots: list[str] = Field(default_factory=list)


class AdmissionRefusal(BaseModel):
    """Worker-side refusal payload posted to /jobs/{id}/refuse-admission.

    Fields mirror `gpu_admission_check` in the worker — `required_gb` is
    what the matcher said the job needs, `free_gb` is the live nvidia-smi
    reading at the moment of decision, `foreign_*` identifies the
    non-jobd-owned holders driving the contention.
    """

    required_gb: float
    free_gb: float
    foreign_pids: list[int] = Field(default_factory=list)
    foreign_vram_gb: float = 0.0


class ClassifyRequest(BaseModel):
    cmd: str


class ClassifyResult(BaseModel):
    heavy: bool
    rule_id: str | None = None
    suggest_profile: str | None = None
    confidence: Literal["high", "medium", "low"] | None = None
    reason: str | None = None


class EventIngest(BaseModel):
    source: Literal["worker", "hook", "mcp"]
    event: str
    job_id: int | None = None
    project: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


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


# Sources for a resolved field, in precedence order from highest (cli) to
# lowest (global). Surfaced via /resolve so callers can debug *why* a job
# wound up with a particular value.
ResolutionSource = Literal["cli", "project_default", "profile", "global"]


class FieldResolution(BaseModel):
    value: Any
    source: ResolutionSource


class ResolvedConfig(BaseModel):
    """The effective per-job config the broker would apply at submit time,
    plus the source of each field. Returned by POST /resolve. No Job row is
    created.
    """

    project: str
    effective_priority: FieldResolution
    effective_host_pin: FieldResolution
    effective_max_wall_s: FieldResolution
    effective_idle_timeout_s: FieldResolution
    effective_checkpoint_grace_s: FieldResolution
    effective_preemptible: FieldResolution
    effective_requires: FieldResolution
    effective_escalate_to_arc: FieldResolution
    submit_warning: str | None = None
