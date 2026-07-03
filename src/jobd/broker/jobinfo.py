"""Job ORM row -> JobInfo response model, with per-request ETA population."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select

from jobd.ctest_eta import predict_ctest
from jobd.db import Job
from jobd.estimator import (
    WallPrediction,
    cmd_head,
    make_predict_cache,
    queue_start_eta,
    remaining_for_running,
)
from jobd.models import JobInfo, JobRequires, JobState

# Placeholder that replaces env VALUES on observability read surfaces. Keys are
# preserved (operators can still see WHICH vars a job sets) but values are hidden.
ENV_REDACTED_PLACEHOLDER = "***"


def _redact_env(env: dict[str, str]) -> dict[str, str]:
    """Mask env values (keys preserved) for read surfaces that must not echo
    submitted secrets. Submitted `env` can carry tokens; `GET /jobs`,
    `GET /jobs/{id}`, submit/cancel/preempt responses, and the MCP
    status/get tools (which read through those endpoints) all flow through
    `_to_info` and would otherwise return the plaintext values. Only the
    worker-claim `/next-job` path passes ``redact_env=False`` so the job still
    runs with the real values (audit 2026-07-01, LOW-Sec `--env`)."""
    return {k: ENV_REDACTED_PLACEHOLDER for k in env}


def _to_info(job: Job, eta_ctx: dict | None = None, *, redact_env: bool = True) -> JobInfo:
    req = None
    if job.requires_json and job.requires_json != "{}":
        try:
            req = JobRequires.model_validate_json(job.requires_json)
        except Exception:
            req = None
    env_dict = json.loads(job.env_json or "{}")
    info = JobInfo(
        id=job.id,
        project=job.project,
        profile=job.profile,
        host_pin=job.host_pin,
        priority=job.priority,
        state=JobState(job.state),
        cmd=json.loads(job.cmd_json),
        cwd=job.cwd,
        preemptible=job.preemptible,
        worker=job.worker,
        submitted_at=job.submitted_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        exit_code=job.exit_code,
        vram_gb=job.vram_gb,
        ram_gb=job.ram_gb,
        cpus=job.cpus,
        env=_redact_env(env_dict) if redact_env else env_dict,
        requires=req,
        warning=job.warning,
        depends_on=json.loads(job.depends_on_json or "[]"),
        depends_on_any_exit=job.depends_on_any_exit,
        session_id=job.session_id,
        fast_path=bool(job.fast_path),
        max_wall_s=job.max_wall_s,
        idle_timeout_s=job.idle_timeout_s,
        checkpoint_grace_s=job.checkpoint_grace_s,
        scheduling_timeout_s=job.scheduling_timeout_s,
        termination_reason=job.termination_reason,
        array_id=job.array_id,
        array_index=job.array_index,
        array_size=job.array_size,
        submitted_via=job.submitted_via if job.submitted_via in ("cli", "mcp") else None,  # type: ignore[arg-type]
    )
    if eta_ctx is not None and job.state in {
        JobState.QUEUED.value,
        JobState.ASSIGNED.value,
        JobState.RUNNING.value,
    }:
        _populate_eta(info, job, eta_ctx)
    return info


def _populate_eta(info: JobInfo, job: Job, eta_ctx: dict) -> None:
    """Fill in eta_* fields on a non-terminal JobInfo from a per-request cache.

    eta_ctx keys: "cache" (PredictCache), "queued" (list[Job]), "running"
    (list[Job]), "now" (datetime). Built by callers once per request so
    list endpoints don't re-query history per row.
    """
    cache = eta_ctx["cache"]
    cmd = json.loads(job.cmd_json)
    head = cmd_head(cmd)
    ctest_pred = predict_ctest(cmd, job.cwd)
    if ctest_pred is not None:
        info.eta_basis = ctest_pred.basis
        info.eta_p50_s = ctest_pred.sum_cost_s
        info.eta_p90_s = ctest_pred.sum_cost_s
        return
    pred = cache.get(job.project, head)
    info.eta_basis = pred.basis
    if not isinstance(pred, WallPrediction):
        return
    info.eta_p50_s = pred.p50_s
    info.eta_p90_s = pred.p90_s
    info.eta_clipped = pred.clipped

    if job.state == JobState.RUNNING.value:
        rem_p50, rem_p90 = remaining_for_running(job, pred, eta_ctx["now"])
        info.eta_remaining_p50_s = rem_p50
        info.eta_remaining_p90_s = rem_p90
    elif job.state == JobState.QUEUED.value:
        start = queue_start_eta(
            job,
            eta_ctx["queued"],
            eta_ctx["running"],
            cache,
            eta_ctx["now"],
        )
        if start is not None:
            info.eta_start_p50_s = start


def _build_eta_ctx(session) -> dict:
    """Per-request ETA context: prediction cache + active job snapshots."""
    queued = list(session.execute(select(Job).where(Job.state == JobState.QUEUED.value)).scalars())
    running = list(
        session.execute(select(Job).where(Job.state == JobState.RUNNING.value)).scalars()
    )
    return {
        "cache": make_predict_cache(session),
        "queued": queued,
        "running": running,
        "now": datetime.now(UTC),
    }
