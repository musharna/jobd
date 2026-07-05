"""Pydantic request/response models for the jobd HTTP API."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)


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
    ORPHANED = "orphaned"
    # Audit 2026-05-18 (runtime-zombies S3): terminal state for jobs whose
    # caller-supplied scheduling_timeout_s elapsed while still queued. Pattern
    # borrowed from Hatchet's scheduling_timeout; jobs 577/578 on server motivated
    # this — capability-mismatch echoes that would never dispatch and had no
    # DLQ. Only fires when scheduling_timeout_s is explicitly set.
    SCHEDULING_TIMEOUT = "scheduling_timeout"


# Canonical set of states from which no further transition happens. Defined
# once here (JobState's home) so the broker, CLI, and MCP surfaces can't drift
# apart — a partial copy makes `job wait` / `job status --watch` / MCP
# wait=true hang forever on a preempted/orphaned/scheduling_timeout job (the
# single-job CLI/MCP copies were the narrow {completed, failed, cancelled}).
# JobState is a StrEnum, so members compare and hash equal to their string
# values: `"orphaned" in TERMINAL_STATES` works on raw status strings from the
# HTTP API just as `JobState.ORPHANED in TERMINAL_STATES` works on enum values.
TERMINAL_STATES: frozenset[JobState] = frozenset(
    {
        JobState.COMPLETED,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.PREEMPTED,
        JobState.ORPHANED,
        JobState.SCHEDULING_TIMEOUT,
    }
)

# Terminal states that are a non-success outcome (everything terminal except
# COMPLETED) — used for ✗-vs-✓ rendering and failure-cascade checks.
TERMINAL_FAIL_STATES: frozenset[JobState] = TERMINAL_STATES - {JobState.COMPLETED}


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


class SweepAxis(BaseModel):
    """One named axis of a parameter sweep: a key and the values it ranges over.

    The broker takes the cartesian product of all axes to fan out array members;
    each member substitutes `{key}` → its value in the command and env. `{i}`
    (the flat member index) is always available alongside the named keys, so
    `i` is reserved and rejected as an axis key. See jobd.arrays.
    """

    model_config = ConfigDict(extra="forbid")

    key: str = Field(..., min_length=1)
    values: list[str] = Field(..., min_length=1)


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
    # Job arrays: submit N members from one template in a single call. Each
    # member is a normal job; any `{i}` in a cmd arg or env value is replaced by
    # the member's 0-based index (0..count-1). count=1 (default) is an ordinary
    # single job and the response is a single JobInfo; count>1 returns an array
    # summary. The upper bound guards against accidental runaway fan-out. See
    # jobd.arrays.
    count: int = Field(default=1, ge=1, le=1000)
    # Job arrays via parameter sweep: instead of a bare `{i}` index, expand the
    # cartesian product of named axes (e.g. lr=[0.1,0.01] × seed=[1,2,3] → 6
    # members), substituting each `{key}` into the command/env. `{i}` (flat
    # 0-based member index) is always available too. Mutually exclusive with
    # count>1; the product is capped at 1000 to match count's bound. Empty
    # (the default) = no sweep. See jobd.arrays.sweep_member_subs.
    sweep: list[SweepAxis] = Field(default_factory=list)
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

    @model_validator(mode="after")
    def _check_sweep(self) -> JobSubmit:
        if not self.sweep:
            return self
        # Sweep and --count are two surfaces for the same array machinery;
        # combining them is ambiguous (does {i} mean sweep-flat-index or
        # count-index?). Reject rather than guess.
        if self.count != 1:
            raise ValueError("sweep and count are mutually exclusive")
        keys = [ax.key for ax in self.sweep]
        if "i" in keys:
            raise ValueError("'i' is reserved for the member index; use a different sweep key")
        if len(set(keys)) != len(keys):
            raise ValueError("sweep axis keys must be unique")
        total = 1
        for ax in self.sweep:
            total *= len(ax.values)
        if total > 1000:
            raise ValueError(f"sweep expands to {total} members; cap is 1000")
        return self


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
    # Job-array grouping (all None for a standalone job). array_id is the first
    # member's job id; array_index is 0-based; array_size is the member count.
    array_id: int | None = None
    array_index: int | None = None
    array_size: int | None = None
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
    # Slot usage for multislotting: `running` jobs out of `max_concurrent`.
    max_concurrent: int = 1
    running: int = 0

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
    # Multislotting observability: the worker's JOBD_WORKER_MAX_CONCURRENT_JOBS
    # and its live in-flight job count. Defaults (1/0) keep pre-field workers
    # reading as single-slot, idle.
    max_concurrent: int = 1
    running: int = 0
    # SIGTERM-drain Phase 2 (docs/plans/sigterm-drain.md): the job ids this
    # worker currently holds in flight. None = old worker that doesn't report;
    # the broker's heartbeat reconcile skips it entirely. An empty list is a
    # real claim of "nothing in flight" and DOES reconcile.
    in_flight_job_ids: list[int] | None = None
    # Issue #7 (per-job PID inventory): {job_id: [pids]} — each job's
    # top-level pid plus its live scope-cgroup pids. Feeds /gpu-holders'
    # known/job_id/worker attribution. None = old worker that doesn't report.
    in_flight_pids: dict[str, list[int]] | None = None


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
    # Server-side long-poll: seconds the broker may hold this request waiting for
    # a dispatchable job before returning null. 0 (default) = legacy instant
    # return, so older workers and tests that don't set it are unaffected.
    wait_s: float = 0.0


class AdmissionRefusal(BaseModel):
    """Worker-side refusal payload posted to /jobs/{id}/refuse-admission.

    Fields mirror `gpu_admission_check` in the worker — `required_gb` is
    what the matcher said the job needs, `free_gb` is the live nvidia-smi
    reading at the moment of decision, `foreign_*` identifies the
    non-jobd-owned holders driving the contention.
    """

    # reason routes the broker's handling: "gpu_contention" (default, the
    # original VRAM-admission path) or "cwd_missing" (the worker found the job's
    # cwd absent on this host -> exclude this host + re-route, or fail unreachable).
    reason: str = "gpu_contention"
    cwd: str | None = None
    # Optional so a cwd_missing refusal can omit them; the GPU path still sends both.
    required_gb: float | None = None
    free_gb: float | None = None
    foreign_pids: list[int] = Field(default_factory=list)
    foreign_vram_gb: float = 0.0


class CompletePayload(BaseModel):
    """Worker's terminal report to POST /jobs/{id}/complete.

    Typed for the same reason the config-mutation endpoints are (audit
    2026-07-01 #25 / 2026-07-05): an untyped dict let a malformed worker
    payload store a non-int exit_code in an int column, and a missing body
    raised an opaque 422 with no field names. final_state still gets its
    richer terminal-set validation in the handler (400 with the allowed set)."""

    exit_code: int | None = None
    final_state: str | None = None
    termination_reason: str | None = None


class ClassifyRequest(BaseModel):
    cmd: str


class SetPriorityRequest(BaseModel):
    """Body for POST /projects/{name}. A pydantic model (not a raw dict) so a
    missing/non-integer `priority` fails validation with 422 instead of the
    handler raising KeyError/ValueError -> 500. The [0,100] clamp stays in the
    handler (out-of-range is clamped, not rejected, preserving prior behavior)."""

    priority: int


class NudgePriorityRequest(BaseModel):
    """Body for POST /projects/{name}/nudge. See SetPriorityRequest re: 422."""

    delta: int


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

    # Envelope fields _emit_event sets itself (ts server-side; the rest are
    # explicit params). A payload key shadowing one used to blow up the call
    # with "got multiple values for keyword argument" — an uncaught 500. 422
    # instead; it also closes the only route to forging envelope fields.
    _RESERVED_ENVELOPE_KEYS: ClassVar[frozenset[str]] = frozenset(
        {"ts", "source", "event", "job_id", "project"}
    )

    @field_validator("payload")
    @classmethod
    def _reject_reserved_envelope_keys(cls, v: dict[str, Any]) -> dict[str, Any]:
        collisions = cls._RESERVED_ENVELOPE_KEYS.intersection(v)
        if collisions:
            raise ValueError(
                f"payload keys collide with reserved event envelope fields: {sorted(collisions)}"
            )
        return v


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
