"""FastAPI application factory for jobd.

All endpoints in this file for now; split into submodules when it grows past
~500 lines or gets distinct subsystems.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sse_starlette.sse import EventSourceResponse

from jobd import __version__
from jobd import events as _events
from jobd.arrays import index_subs, render_cmd, render_env, sweep_member_subs
from jobd.auth import install_tailnet_acl, require_token
from jobd.config import (
    ClassifierRule,
    ProjectEntry,
    load_classifier_rules,
    load_profiles,
    load_projects,
    resolve_priority,
    resolve_profile,
    resolve_project_defaults,
)
from jobd.ctest_eta import predict_ctest
from jobd.db import Job, Worker, init_db, migrate
from jobd.estimator import (
    WallPrediction,
    cmd_head,
    make_predict_cache,
    queue_start_eta,
    remaining_for_running,
)
from jobd.matcher import (
    WorkerSnapshot,
    cwd_routability,
    eligible_workers,
    gpu_contention_warning,
    selectors_only_match,
    submit_preflight,
)
from jobd.models import (
    AdmissionRefusal,
    ClassifyRequest,
    ClassifyResult,
    EventIngest,
    FieldResolution,
    JobInfo,
    JobRequires,
    JobState,
    JobSubmit,
    NextJobQuery,
    ProfileSpec,
    ResolutionSource,
    ResolvedConfig,
    WorkerHeartbeat,
    WorkerInfo,
)

log = logging.getLogger("jobd")


class BrokerState(TypedDict):
    """Shared, mutable per-broker config + scratch, stored on app.state.shared.

    Typed so the heterogeneous values resolve to their real types instead of
    the `object` join mypy would otherwise infer from a bare dict literal.
    """

    projects: dict[str, ProjectEntry]
    profiles: dict[str, ProfileSpec]
    classifier: list[ClassifierRule]
    paths: dict[str, Path]
    logs_dir: Path
    dispatch_skip_state: dict[int, str]


TERMINAL_STATES = {
    JobState.COMPLETED,
    JobState.FAILED,
    JobState.CANCELLED,
    JobState.PREEMPTED,
    JobState.ORPHANED,
    JobState.SCHEDULING_TIMEOUT,
}

DEAD_WORKER_SECONDS = 300  # 5 min
IDEMPOTENT_RECLAIM_SECONDS = 90
OFFLINE_AFTER_SECONDS = 120
# SIGTERM-drain Phase 2 (docs/plans/sigterm-drain.md): heartbeat reconcile.
# A claimed (ASSIGNED/RUNNING) job must be absent from this many CONSECUTIVE
# in_flight_job_ids reports, and be at least this old since its claim, before
# it gets the worker-died disposition. The age floor plus the debounce cover
# the /next-job -> first-report and /complete-in-flight races.
RECONCILE_MISS_THRESHOLD = 2
RECONCILE_MIN_AGE_SECONDS = 60
# Broker-side wall-clock backstop grace (RUNNING reaper). Worker-side
# enforcement (job_worker.poll_signals) SIGTERMs at exactly max_wall_s and
# reports within seconds. The broker only needs to act when that enforcement
# never lands (the worker crashed mid-run and restarted with no memory of the
# job). This grace keeps the broker strictly looser than a healthy worker so it
# never races one — it fires only on a genuinely stranded job. job 1364 (2026-06).
WALL_CLOCK_BACKSTOP_GRACE_SECONDS = 120
# Audit 2026-05-18 (runtime-zombies S6): shorter threshold than OFFLINE_AFTER
# so a crashed-mid-run worker stops being matcher-eligible quickly. Probe
# finding: 2 stale workers on server. Configurable via env so operators can
# tune for heartbeat cadence.
STALE_WORKER_THRESHOLD_S_DEFAULT = 60
SWEEP_INTERVAL_SECONDS = 30
# Job/log retention: terminal jobs (and their per-job .log files) whose
# finished_at is older than this many days are pruned by the sweeper. 0
# (default) keeps history forever — opt-in so existing deployments never
# silently lose job records. Override via JOBD_JOB_RETENTION_DAYS. Bounds
# jobs-table + log-dir growth; events.jsonl is bounded separately by rotation.
JOB_RETENTION_DAYS_DEFAULT = 0
# /next-job long-poll: a waiting worker re-attempts the pick at least this often
# even with no wake, so a missed wake site costs at most this much latency (not
# the full wait_s). Small enough to be a safe backstop, large enough that an
# idle worker held on the condition isn't busy-querying.
_LONGPOLL_RECHECK_S = 10.0
UNMATCHEABLE_THRESHOLD_SECONDS = 60
QUEUE_BLOCKED_THRESHOLD_SECONDS = 300  # 5 min: above this, surface load-blocker
AUTO_PREEMPT_MIN_RUNTIME_SECONDS = 300  # don't auto-preempt jobs that just started
MAX_LOG_CHUNK_BYTES = 10 * 1024 * 1024  # 10 MiB cap per /log append

_UNMATCHEABLE_WARNING_PREFIX = "no matching worker —"
_BLOCKED_WARNING_PREFIX = "queue-age "
_AUTO_PREEMPT_WARNING_PREFIX = "auto-preempted in favor of job "

_SINCE_RELATIVE_RE = re.compile(r"^(\d+)([hdw])$")


def _parse_since(s: str) -> datetime:
    """Parse '24h'/'7d'/'2w' relative or ISO-8601 absolute → cutoff datetime (UTC).

    Events with ts >= cutoff are included. Mirrors the positive-only guard
    of job_cli.cli._parse_since.
    """
    s = s.strip()
    m = _SINCE_RELATIVE_RE.match(s)
    if m:
        n = int(m.group(1))
        if n <= 0:
            raise HTTPException(status_code=400, detail=f"invalid since={s!r}: must be positive")
        unit = m.group(2)
        delta = {
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
            "w": timedelta(weeks=n),
        }[unit]
        return datetime.now(UTC) - delta
    # Python ≥3.11 fromisoformat accepts trailing Z, but be defensive on older
    # variants by normalising it explicitly.
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"invalid since={s!r}: {e}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _emit_event(
    logs_dir: Path,
    event: str,
    *,
    source: str,
    job_id: int | None = None,
    project: str | None = None,
    **payload,
) -> None:
    """Append a single schema-v2 JSON line to logs_dir/events.jsonl.

    Schema: {ts, source, event, job_id, project, payload}.
    Best-effort: errors are logged at WARNING and swallowed so observability
    never breaks broker liveness.
    """
    row = {
        "ts": datetime.now(UTC).isoformat(),
        "source": source,
        "event": event,
        "job_id": job_id,
        "project": project,
        "payload": payload,
    }
    try:
        _events.append_event(logs_dir, row)
    except Exception as e:
        log.warning("event emit failed (%s): %s", event, e)


def build_app(
    db_url: str,
    projects_path: Path | str,
    profiles_path: Path | str,
    classifier_path: Path | str,
    logs_path: Path | str | None = None,
) -> FastAPI:
    async def _sweep_loop():
        while True:
            try:
                _sweep_once()
            except Exception as e:
                log.warning("sweeper error: %s", e)
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(_sweep_loop())
        yield
        task.cancel()

    app = FastAPI(
        title="jobd",
        version=__version__,
        lifespan=lifespan,
        dependencies=[Depends(require_token)],
    )
    install_tailnet_acl(app)

    engine = create_engine(db_url, future=True)

    # SQLite tuning: WAL lets the broker's readers (CLI/MCP polls, ETA queries)
    # proceed concurrently with worker writes instead of blocking on a single
    # global lock; busy_timeout makes a contended write wait up to 5s rather
    # than raising SQLITE_BUSY immediately; synchronous=NORMAL is the safe WAL
    # companion (durable across app crashes, only at risk on OS/power loss).
    # Applied per-connection (idempotent) and only for SQLite backends.
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _conn_record):  # pragma: no cover - driver cb
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

    init_db(engine)
    migrate(engine)
    SessionLocal = sessionmaker(engine, expire_on_commit=False)

    logs_dir = Path(logs_path) if logs_path else Path(os.environ.get("JOBD_LOGS_DIR", "./logs"))
    logs_dir.mkdir(parents=True, exist_ok=True)

    state: BrokerState = {
        "projects": load_projects(projects_path),
        "profiles": load_profiles(profiles_path),
        "classifier": load_classifier_rules(classifier_path),
        "paths": {
            "projects": Path(projects_path),
            "profiles": Path(profiles_path),
            "classifier": Path(classifier_path),
        },
        "logs_dir": logs_dir,
        # Per-(job_id) dedup map for dispatch_skip emission. Keyed by job_id,
        # value is the most-recently-emitted reason string. Reset on broker
        # restart (best-effort observability — see Task 4 brief).
        "dispatch_skip_state": {},
    }
    app.state.shared = state
    app.state.SessionLocal = SessionLocal

    # Server-side long-poll wake signal. /next-job with wait_s>0 blocks on this
    # condition until a job may have become dispatchable (submit / terminal
    # transition / requeue) or its deadline elapses, instead of returning
    # instantly and forcing the worker to re-poll every 2s. notify_all is cheap
    # and spurious wakes are harmless (the waiter just re-runs the pick and
    # re-waits), so we wake liberally on any mutation that could newly satisfy a
    # match. A bounded internal recheck (_LONGPOLL_RECHECK_S) backstops any wake
    # site we miss, capping worst-case dispatch latency without a network poll.
    _job_wake = threading.Condition()

    def _wake_dispatchers() -> None:
        with _job_wake:
            _job_wake.notify_all()

    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/gpu-holders")
    def gpu_holders(host: str | None = None):
        """Audit 2026-05-18 (runtime-zombies S5): dual-signal GPU-holder
        probe. NVML compute-apps unioned with `fuser -v /dev/nvidia*` so
        the desktop-inference_server failure mode (NVML returns [N/A], only
        fuser sees the process) is observable.

        Returns [{pid, gpu_id, mem_mb, source, known, job_id, worker}].
        source ∈ {nvml, fuser, both}. `known` is True when the PID appears
        in a worker's heartbeat-reported per-job PID inventory (#7);
        job_id/worker say which job claims it. Note: the probe is
        broker-local — it probes the BROKER host, which is almost never the
        GPU host. Useful primarily when running directly on a worker.

        PIDs are host-local, so pass `?host=<worker>` to consult only that
        worker's inventory. The default consults every online worker, which
        can mis-attribute on a numeric pid collision across hosts — fine on
        a broker+worker single host, caveat emptor on a fleet.
        """
        from jobd.gpu_holder_probe import probe_gpu_holders

        pid_owner: dict[int, tuple[int, str]] = {}
        with SessionLocal() as session:
            q = select(Worker).where(Worker.state == "online")
            if host is not None:
                q = q.where(Worker.host == host)
            for w in session.execute(q).scalars():
                try:
                    inventory = json.loads(w.in_flight_pids_json or "{}")
                except ValueError:
                    continue
                for jid, pids in inventory.items():
                    for p in pids:
                        pid_owner.setdefault(int(p), (int(jid), w.host))
        return [
            {
                "pid": h.pid,
                "gpu_id": h.gpu_id,
                "mem_mb": h.mem_mb,
                "source": h.source,
                "known": h.known,
                "job_id": pid_owner[h.pid][0] if h.pid in pid_owner else None,
                "worker": pid_owner[h.pid][1] if h.pid in pid_owner else None,
            }
            for h in probe_gpu_holders(known_pids=set(pid_owner))
        ]

    # response_model=None: dry-run returns a plan dict, live returns JobInfo.
    # Both paths are JSON-serializable; FastAPI dispatches per-response.
    @app.post("/submit", response_model=None)
    def submit(req: JobSubmit):
        profile_spec = None
        if req.profile:
            profile_spec = resolve_profile(state["profiles"], req.profile)
            if profile_spec is None:
                raise HTTPException(status_code=404, detail=f"unknown profile: {req.profile}")

        priority = resolve_priority(state["projects"], req.project, req.priority_delta)
        # Per docs/projects-yaml.md §3, resolution order is:
        # CLI > project_default > profile > global. The `req.<field>` here is
        # the CLI-side value; project defaults sit just below.
        proj_defaults = resolve_project_defaults(state["projects"], req.project)

        host_pin = req.host_pin
        if host_pin == "any" and proj_defaults.host_pin:
            host_pin = proj_defaults.host_pin
        elif host_pin == "any" and profile_spec and profile_spec.host_hint:
            host_pin = profile_spec.host_hint

        # Explicit `req.vram_gb > 0` wins; otherwise fall through to the
        # profile spec; otherwise 0 (matcher applies tier-tag / implicit-
        # floor fallback). See JobSubmit.vram_gb docstring + matcher's
        # `effective_vram_request_gb`.
        vram_gb = req.vram_gb if req.vram_gb > 0 else (profile_spec.vram_gb if profile_spec else 0)
        ram_gb = profile_spec.ram_gb if profile_spec else 0
        cpus = profile_spec.cpus if profile_spec else 1

        preemptible = req.preemptible
        if preemptible is None:
            preemptible = proj_defaults.preemptible
        if preemptible is None and profile_spec is not None:
            preemptible = profile_spec.preemptible
        if preemptible is None:
            preemptible = False

        if req.fast_path is not None:
            fast_path = req.fast_path
        else:
            fast_path = profile_spec.fast_path if profile_spec else False

        requires = req.requires
        if requires is None:
            requires = proj_defaults.requires
        if requires is None and profile_spec and profile_spec.requires:
            requires = profile_spec.requires
        requires_json = requires.model_dump_json() if requires is not None else "{}"

        max_wall_s = req.max_wall_s
        if max_wall_s is None:
            max_wall_s = proj_defaults.max_wall_s

        idle_timeout_s = req.idle_timeout_s
        if idle_timeout_s is None:
            idle_timeout_s = proj_defaults.idle_timeout_s

        checkpoint_grace_s = req.checkpoint_grace_s
        if checkpoint_grace_s is None:
            checkpoint_grace_s = proj_defaults.checkpoint_grace_s

        # Unknown-project warning: surface (don't refuse) so first-time
        # `--project new-experiment` keeps working with global defaults.
        unknown_project_warning: str | None = None
        if req.project not in state["projects"] and "_default" in state["projects"]:
            unknown_project_warning = (
                f"project {req.project!r} has no entry in projects.yaml; using global defaults"
            )

        # cwd sanity: Windows-mount paths only exist on the laptop (WSL). If
        # someone submits --cwd /mnt/c/... without pinning the laptop, the
        # worker will fail cd and every process will rc=127. Root cause of the
        # 2026-04-22 project-b storm.
        if req.cwd.startswith("/mnt/c/") and host_pin not in ("laptop", "MSI", "any-laptop"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"cwd {req.cwd!r} is under /mnt/c/ (Windows mount, laptop-only) "
                    f"but host_pin={host_pin!r}. Pass --host laptop, or stage data "
                    f"under a cross-host path like /tmp or a project-scoped dir."
                ),
            )

        with SessionLocal() as session:
            for dep_id in req.depends_on:
                if session.get(Job, dep_id) is None:
                    raise HTTPException(
                        status_code=400, detail=f"depends_on refers to missing job: {dep_id}"
                    )
            now = datetime.now(UTC)
            # Warnings are routing-derived (requires / host_pin / live capacity),
            # so a job array's members share one warning string — the per-member
            # `{i}` substitution only changes the command, not where it routes.
            online = list(
                session.execute(select(Worker).where(Worker.state == "online")).scalars().all()
            )
            snapshots = _build_snapshots(online)
            all_workers = list(session.execute(select(Worker)).scalars().all())
            all_snapshots = _build_snapshots(all_workers)
            ser_warn = _serialization_warning(requires, host_pin, snapshots, session)
            gpu_warn = gpu_contention_warning(requires, host_pin, snapshots)
            preflight_warn = submit_preflight(requires, host_pin, all_snapshots)
            cwd_route = cwd_routability(req.cwd, host_pin, all_snapshots)
            cwd_route_warn: str | None = None
            if cwd_route is not None:
                is_hard, cwd_msg = cwd_route
                if is_hard:
                    raise HTTPException(status_code=400, detail=cwd_msg)
                cwd_route_warn = cwd_msg
            warnings = [
                w
                for w in (
                    unknown_project_warning,
                    preflight_warn,
                    cwd_route_warn,
                    ser_warn,
                    gpu_warn,
                )
                if w is not None
            ]
            warning_text = "; ".join(warnings) if warnings else None

            # Resolve the per-member substitutions up front so dry-run reports
            # the same member count the live submit would create. Three forms,
            # mutually exclusive (sweep+count rejected at the model):
            #   sweep   → cartesian product of named axes, each carrying {i}
            #   count>1 → bare {i} = 0..count-1
            #   neither → a single ordinary job (NULL array columns)
            if req.sweep:
                member_subs = sweep_member_subs([(ax.key, ax.values) for ax in req.sweep])
            elif req.count > 1:
                member_subs = [index_subs(i) for i in range(req.count)]
            else:
                member_subs = None
            is_array = member_subs is not None
            subs_list = member_subs if member_subs is not None else [{}]
            member_count = len(subs_list)

            # Dry-run bail-out:
            # full validation + routing-decision has run above (profile lookup,
            # project defaults, cwd sanity, depends_on existence, preflight,
            # gpu_contention). Return the would-be plan WITHOUT inserting any
            # Job row or emitting `job_submitted`. One plan covers every array
            # member (routing ignores the per-member substitution); array_count
            # tells the caller how many members the live submit would create.
            if req.dry_run:
                eligible = eligible_workers(requires, host_pin, snapshots)
                # would_route_to: hostnames the matcher would currently
                # consider (capability + host_pin match, ignores load). Empty
                # list = unmatcheable right now (preflight_warn explains why).
                would_route_to = [w.host for w in eligible]
                # would_use_worker: pre-load tie-break is matcher-internal; at
                # preview time we surface "any of these candidates" rather
                # than promising a specific host. None when no candidates.
                would_use_worker = eligible[0].host if len(eligible) == 1 else None
                return {
                    "state": "dry-run",
                    "would_route_to": would_route_to,
                    "would_use_worker": would_use_worker,
                    "array_count": member_count,
                    "validation": {
                        "effective_priority": priority,
                        "effective_host_pin": host_pin,
                        "effective_preemptible": preemptible,
                        "effective_vram_gb": vram_gb,
                        "effective_ram_gb": ram_gb,
                        "effective_cpus": cpus,
                        "effective_max_wall_s": max_wall_s,
                        "effective_idle_timeout_s": idle_timeout_s,
                        "effective_checkpoint_grace_s": checkpoint_grace_s,
                        "effective_scheduling_timeout_s": req.scheduling_timeout_s,
                        "effective_fast_path": fast_path,
                        "effective_requires": (
                            requires.model_dump() if requires is not None else None
                        ),
                        "warnings": warnings,
                    },
                }

            # Fan out into members using the substitutions resolved above. A
            # non-array submit (member_subs is None) is one ordinary job with
            # NULL array columns and an unchanged single-JobInfo response.
            members: list[Job] = []
            for i, subs in enumerate(subs_list):
                member_cmd = render_cmd(req.cmd, subs) if is_array else req.cmd
                member_env = render_env(req.env, subs) if is_array else req.env
                job = Job(
                    project=req.project,
                    profile=req.profile,
                    host_pin=host_pin,
                    priority=priority,
                    state=JobState.QUEUED,
                    cmd_json=json.dumps(member_cmd),
                    cwd=req.cwd,
                    env_json=json.dumps(member_env),
                    preemptible=preemptible,
                    vram_gb=vram_gb,
                    ram_gb=ram_gb,
                    cpus=cpus,
                    session_id=req.session_id,
                    submitted_at=now,
                    requires_json=requires_json,
                    depends_on_json=json.dumps(req.depends_on),
                    depends_on_any_exit=req.depends_on_any_exit,
                    fast_path=fast_path,
                    max_wall_s=max_wall_s,
                    idle_timeout_s=idle_timeout_s,
                    checkpoint_grace_s=checkpoint_grace_s,
                    scheduling_timeout_s=req.scheduling_timeout_s,
                    submitted_via=req.submitted_via,
                    array_index=i if is_array else None,
                    array_size=member_count if is_array else None,
                )
                if warning_text:
                    job.warning = warning_text
                    job.warning_at = now
                session.add(job)
                members.append(job)

            # Flush to assign ids; the array shares the first member's id so
            # `job status A<id>` resolves without a separate id sequence.
            session.flush()
            member_ids = [j.id for j in members]
            array_id = member_ids[0] if is_array else None
            if is_array:
                for j in members:
                    j.array_id = array_id
            session.commit()

            # Emit events from captured scalars (no post-commit ORM reload per
            # member — every member shares project/priority/host_pin/warning).
            for jid in member_ids:
                _emit_event(
                    logs_dir,
                    "job_submitted",
                    source="broker",
                    job_id=jid,
                    project=req.project,
                    priority=priority,
                    host_pin=host_pin,
                    preemptible=preemptible,
                    warning=warning_text,
                )
                if warning_text:
                    _emit_event(
                        logs_dir,
                        "submit_warning",
                        source="broker",
                        job_id=jid,
                        project=req.project,
                        warning_text=warning_text,
                    )

            # New queued job(s) — wake any worker long-polling /next-job.
            _wake_dispatchers()
            if is_array:
                return {
                    "array_id": array_id,
                    "count": member_count,
                    "job_ids": member_ids,
                    "warnings": warnings,
                }
            eta_ctx = _build_eta_ctx(session)
            return _to_info(members[0], eta_ctx)

    @app.get("/jobs", response_model=list[JobInfo])
    def list_jobs(
        state_filter: str | None = None,
        project: str | None = None,
        warnings_only: bool = False,
        array_id: int | None = None,
    ):
        with SessionLocal() as session:
            stmt = select(Job).order_by(Job.id.desc())
            if state_filter:
                stmt = stmt.where(Job.state == state_filter)
            if project:
                stmt = stmt.where(Job.project == project)
            if warnings_only:
                stmt = stmt.where(Job.warning.is_not(None))
            if array_id is not None:
                # array members are ordered by index for readable `--array` output
                stmt = stmt.where(Job.array_id == array_id).order_by(None).order_by(Job.array_index)
            jobs = session.execute(stmt).scalars().all()
            eta_ctx = _build_eta_ctx(session)
            return [_to_info(j, eta_ctx) for j in jobs]

    @app.get("/jobs/{job_id}", response_model=JobInfo)
    def get_job(job_id: int):
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            eta_ctx = _build_eta_ctx(session)
            return _to_info(job, eta_ctx)

    @app.post("/jobs/{job_id}/log")
    async def append_log(job_id: int, request: Request):
        with SessionLocal() as session:
            if session.get(Job, job_id) is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
        body = await request.body()
        if len(body) > MAX_LOG_CHUNK_BYTES:
            raise HTTPException(status_code=413, detail="log chunk too large")
        log_file = logs_dir / f"{job_id}.log"
        with log_file.open("ab") as f:
            f.write(body)
        return {"bytes": len(body)}

    @app.post("/jobs/{job_id}/started", response_model=JobInfo)
    def started_job(job_id: int):
        """Worker reports subprocess.Popen returned: assigned -> running.

        The matcher flips queued->assigned at dispatch and stamps started_at,
        but the actual subprocess launch happens later in the worker (systemd-run
        scope setup can take ~300ms). This endpoint marks the live transition
        so callers see `running` while the job actually executes, and the
        cancel signal_sent synthesis in MCP can tell "queued cancel" from
        "running SIGTERM" cleanly.

        Idempotent: only flips assigned->running. Terminal or already-running
        states are no-ops (the worker may retry on transient network errors).
        """
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            if job.state == JobState.ASSIGNED:
                job.state = JobState.RUNNING
                session.commit()
                session.refresh(job)
                _emit_event(
                    logs_dir,
                    "job_started",
                    source="broker",
                    job_id=job_id,
                    project=job.project,
                    worker=job.worker,
                )
            return _to_info(job)

    @app.post("/jobs/{job_id}/complete", response_model=JobInfo)
    def complete_job(job_id: int, payload: dict):
        exit_code = payload.get("exit_code")
        final_state = payload.get("final_state")
        termination_reason = payload.get("termination_reason")
        if final_state is None:
            # Worker didn't specify — derive from exit code. This keeps the
            # depends_on cascade honest: nonzero rc must land in a failed-side
            # terminal so children that didn't opt into any_exit get cancelled.
            final_state = "completed" if exit_code in (0, None) else "failed"
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            # final_state must name a valid terminal state. Reject garbage so a
            # typo'd worker payload can't strand the job in a non-enum state that
            # later raises ValueError in JobState(job.state) on every read.
            try:
                final = JobState(final_state)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"invalid final_state {final_state!r}; must be one of "
                        f"{sorted(s.value for s in TERMINAL_STATES)}"
                    ),
                ) from None
            if final not in TERMINAL_STATES:
                raise HTTPException(
                    status_code=400,
                    detail=f"final_state {final_state!r} is not a terminal state",
                )
            # Terminal is terminal: a job already in a terminal state ignores a
            # late or duplicate /complete (worker retry, or the user cancelled /
            # the sweeper orphaned it first). Idempotent no-op — don't overwrite
            # the recorded outcome, don't re-cascade to dependents, don't re-emit
            # job_completed.
            if JobState(job.state) in TERMINAL_STATES:
                return _to_info(job)
            job.state = final_state
            job.exit_code = exit_code
            job.finished_at = datetime.now(UTC)
            if termination_reason is not None:
                job.termination_reason = termination_reason
            # Clear any pending signal — it's been honored (or irrelevant now
            # that the job is terminal). Otherwise a future reclaim/retry would
            # see a stale cancel signal and die on startup.
            job.signal = None
            cascaded = _cascade_on_parent_terminal(session, job, logs_dir)
            session.commit()
            session.refresh(job)
            wall_s = None
            if job.started_at is not None and job.finished_at is not None:
                wall_s = round((job.finished_at - job.started_at).total_seconds(), 2)
            _emit_event(
                logs_dir,
                "job_completed",
                source="broker",
                job_id=job_id,
                project=job.project,
                final_state=job.state,
                exit_code=job.exit_code,
                wall_s=wall_s,
                termination_reason=job.termination_reason,
                worker=job.worker,
            )
            _emit_cascade_cancellations(logs_dir, cascaded, job_id, job.state)
            # Terminal transition frees a slot and may unblock dependents — wake.
            _wake_dispatchers()
            return _to_info(job)

    @app.post("/jobs/{job_id}/cancel", response_model=JobInfo)
    def cancel_job(job_id: int):
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            cancel_event_kw: dict | None
            cascaded: list[tuple[int, str]] = []
            if job.state in (JobState.RUNNING, JobState.ASSIGNED):
                prior_state = job.state.value if hasattr(job.state, "value") else job.state
                job.signal = "cancel"
                cancel_event_kw = dict(prior_state=prior_state, by="user_signal_pending")
            elif job.state == JobState.QUEUED:
                prior_state = job.state.value if hasattr(job.state, "value") else job.state
                job.state = JobState.CANCELLED
                job.finished_at = datetime.now(UTC)
                cascaded = _cascade_on_parent_terminal(session, job, logs_dir)
                cancel_event_kw = dict(prior_state=prior_state, by="user")
            else:
                # terminal-state cancel is a no-op; don't emit
                cancel_event_kw = None
            session.commit()
            session.refresh(job)
            if cancel_event_kw is not None:
                _emit_event(
                    logs_dir,
                    "job_cancelled",
                    source="broker",
                    job_id=job_id,
                    project=job.project,
                    **cancel_event_kw,
                )
                _emit_cascade_cancellations(logs_dir, cascaded, job_id, JobState.CANCELLED.value)
                # A cancel can unblock depends_on_any_exit dependents — wake.
                _wake_dispatchers()
            return _to_info(job)

    @app.post("/jobs/{job_id}/preempt", response_model=JobInfo)
    def preempt_job(job_id: int):
        """Preempt a running/assigned job. Worker SIGTERMs the child with
        the same grace flow as cancel; final state will be 'preempted'.
        Refuses if job is not running/assigned (409) or not preemptible (409)."""
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            if job.state not in (JobState.RUNNING, JobState.ASSIGNED):
                raise HTTPException(
                    status_code=409,
                    detail=f"job {job_id} is {job.state} — preempt only applies to running/assigned jobs",
                )
            if not job.preemptible:
                raise HTTPException(
                    status_code=409,
                    detail=f"job {job_id} is not preemptible (submit with --preemptible or set project default)",
                )
            job.signal = "preempt"
            session.commit()
            session.refresh(job)
            return _to_info(job)

    @app.post("/jobs/{job_id}/preempt-blockers")
    def preempt_blockers(job_id: int, force: bool = False):
        """Manual escalation: find a preemptible blocker for queued job
        `job_id` on its eligible workers and signal it now (skipping the
        sweeper's queue-age and runtime guards). With `force=true`, also
        drop the priority guard (preempt blockers at equal-or-higher
        priority — operator override).

        Refuses (409) if the job isn't queued; returns
        `{"signaled": <blocker_id>}` on success or `{"signaled": null,
        "reason": ...}` if no candidate qualifies.
        """
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            if job.state != JobState.QUEUED:
                raise HTTPException(
                    status_code=409,
                    detail=f"job {job_id} is {job.state} — preempt-blockers only applies to queued jobs",
                )
            req: JobRequires | None = None
            if job.requires_json and job.requires_json != "{}":
                try:
                    req = JobRequires.model_validate_json(job.requires_json)
                except Exception:
                    req = None
            workers = (
                session.execute(select(Worker).where(Worker.state == "online")).scalars().all()
            )
            snapshots = _build_snapshots(list(workers))
            elig = eligible_workers(req, job.host_pin, snapshots)
            if not elig:
                return {"signaled": None, "reason": "no eligible workers online"}

            now = datetime.now(UTC)
            hosts = [ws.host for ws in elig]
            stmt = (
                select(Job)
                .where(
                    Job.worker.in_(hosts),
                    Job.state.in_([JobState.ASSIGNED.value, JobState.RUNNING.value]),
                    Job.preemptible.is_(True),
                    Job.signal.is_(None),
                )
                .order_by(Job.priority.asc(), Job.started_at.asc())
                .limit(1)
            )
            if not force:
                stmt = stmt.where(Job.priority < job.priority)
            candidate = session.execute(stmt).scalars().first()
            if candidate is None:
                reason = (
                    "no preemptible candidate on eligible workers"
                    if force
                    else "no preemptible candidate at lower priority on eligible workers (use force=true to override)"
                )
                return {"signaled": None, "reason": reason}

            candidate.signal = "preempt"
            candidate.warning = f"{_AUTO_PREEMPT_WARNING_PREFIX}{job.id} (manual)"
            candidate.warning_at = now
            session.commit()
            _emit_event(
                logs_dir,
                "auto_preempt",
                source="broker",
                job_id=candidate.id,
                project=candidate.project,
                cause="manual_force" if force else "manual",
                queued_job=job.id,
                worker=candidate.worker,
                queued_priority=job.priority,
                candidate_priority=candidate.priority,
            )
            return {"signaled": candidate.id, "worker": candidate.worker}

    @app.get("/jobs/{job_id}/signal")
    def get_signal(job_id: int):
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            return {"signal": job.signal}

    @app.post("/jobs/{job_id}/checkpoint-complete")
    def checkpoint_complete(job_id: int):
        """Worker observed the user's `jobd-checkpoint-complete` token in
        job stdout during a preempt grace window. Pure observability:
        appends a `checkpoint_complete` event to events.jsonl. No state
        change — the actual final state ('preempted'/'failed') still flows
        through /complete after the workload exits.
        """
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            _emit_event(
                logs_dir,
                "checkpoint_complete",
                source="broker",
                job_id=job_id,
                project=job.project,
                worker=job.worker,
            )
            return {"ok": True}

    @app.post("/jobs/{job_id}/refuse-admission", response_model=JobInfo)
    def refuse_admission(job_id: int, payload: AdmissionRefusal):
        """Worker observed live GPU contention (free_vram < required) at the
        moment of dispatch and refuses an already-assigned job. Broker
        reverts state to QUEUED, clears `worker` and `started_at`, and emits
        an `admission_blocked` event. The next /next-job poll re-routes via
        the normal matcher — typically to a different host (the heartbeat
        will catch up within ~5s) or back here once contention clears.

        409 if the job isn't in ASSIGNED state (race with /complete or a
        cancel signal).
        """
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            if job.state != JobState.ASSIGNED:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"job {job_id} is in state {job.state}; "
                        "can't refuse-admission (must be 'assigned')"
                    ),
                )
            prior_worker = job.worker
            job.state = JobState.QUEUED
            job.worker = None
            job.started_at = None
            session.commit()
            _emit_event(
                logs_dir,
                "admission_blocked",
                source="broker",
                job_id=job_id,
                project=job.project,
                worker=prior_worker,
                required_gb=payload.required_gb,
                free_gb=payload.free_gb,
                foreign_pids=list(payload.foreign_pids),
                foreign_vram_gb=payload.foreign_vram_gb,
            )
            # Job is back in QUEUED — wake another worker to re-route it.
            _wake_dispatchers()
            session.refresh(job)
            return _to_info(job)

    @app.get("/jobs/{job_id}/output")
    def get_output(job_id: int, tail: int = 8192):
        """Return the last `tail` bytes of the job's captured stdout+stderr.

        The worker streams log chunks to /log, which the broker appends to
        logs_dir/<id>.log. This endpoint returns a tail of that file so
        callers can diagnose a failed job without SSHing to the worker.
        Returns 404 if the job doesn't exist, empty string if the file
        hasn't been created yet (worker crashed before any output).
        """
        with SessionLocal() as session:
            if session.get(Job, job_id) is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
        log_file = logs_dir / f"{job_id}.log"
        if not log_file.exists():
            return {"tail": "", "size_bytes": 0, "returned_bytes": 0, "truncated": False}
        size = log_file.stat().st_size
        tail = max(0, min(tail, 1_048_576))  # clamp to 1 MiB
        with log_file.open("rb") as f:
            if size > tail:
                f.seek(size - tail)
            data = f.read()
        return {
            "tail": data.decode("utf-8", errors="replace"),
            "size_bytes": size,
            "returned_bytes": len(data),
            "truncated": size > len(data),
        }

    @app.get("/wait/{job_id}")
    async def wait_job(job_id: int):
        log_file = logs_dir / f"{job_id}.log"

        async def event_generator():
            position = 0
            while True:
                # Read new log bytes since last position
                if log_file.exists():
                    with log_file.open("rb") as f:
                        f.seek(position)
                        chunk = f.read()
                    if chunk:
                        position += len(chunk)
                        yield {
                            "event": "log",
                            "data": chunk.decode("utf-8", errors="replace"),
                        }

                # Check job state
                with SessionLocal() as session:
                    job = session.get(Job, job_id)

                if job is None:
                    yield {"event": "error", "data": "no such job"}
                    return

                if JobState(job.state) in TERMINAL_STATES:
                    yield {
                        "event": "terminal",
                        "data": json.dumps({"state": job.state, "exit_code": job.exit_code}),
                    }
                    return

                await asyncio.sleep(0.5)

        return EventSourceResponse(event_generator())

    @app.post("/classify", response_model=ClassifyResult)
    def classify_endpoint(req: ClassifyRequest) -> ClassifyResult:
        from jobd.classifier import classify as _classify

        return _classify(req.cmd, state["classifier"])

    @app.post("/heartbeat")
    def heartbeat(hb: WorkerHeartbeat):
        with SessionLocal() as session:
            worker = session.execute(
                select(Worker).where(Worker.host == hb.host)
            ).scalar_one_or_none()
            is_first_heartbeat = worker is None
            now = datetime.now(UTC)
            if worker is None:
                worker = Worker(host=hb.host, last_heartbeat=now)
                session.add(worker)
            worker.host_aliases_json = json.dumps(hb.host_aliases)
            worker.last_heartbeat = now
            worker.free_vram_gb = hb.free_vram_gb
            worker.unregistered_vram_gb = hb.unregistered_vram_gb
            worker.free_ram_gb = hb.free_ram_gb
            worker.idle_cpus = hb.idle_cpus
            worker.arch = hb.arch
            worker.os = hb.os
            worker.gpu = hb.gpu
            worker.tags_json = json.dumps(hb.tags)
            worker.mount_roots_json = json.dumps(hb.mount_roots)
            worker.max_concurrent = hb.max_concurrent
            worker.running = hb.running
            worker.state = "online"
            if hb.in_flight_pids is not None:
                worker.in_flight_pids_json = json.dumps(hb.in_flight_pids)
            orphan_records: list[tuple[int, str]] = []
            cascade_records: list[tuple[int, str, list[tuple[int, str]]]] = []
            requeued: list[int] = []
            if hb.in_flight_job_ids is not None:
                orphan_records, cascade_records, requeued = _reconcile_worker_in_flight(
                    session, hb.host, set(hb.in_flight_job_ids), logs_dir
                )
            session.commit()
        if is_first_heartbeat:
            _emit_event(
                logs_dir,
                "worker_registered",
                source="broker",
                host=hb.host,
                host_aliases=list(hb.host_aliases),
                tags=list(hb.tags),
                gpu=hb.gpu,
                arch=hb.arch,
                os=hb.os,
                mount_roots=list(hb.mount_roots),
            )
        for jid, proj in orphan_records:
            _emit_event(
                logs_dir,
                "job_orphaned",
                source="broker",
                job_id=jid,
                project=proj,
                worker=hb.host,
                termination_reason="worker_restarted",
            )
        for parent_id, parent_state, cascaded in cascade_records:
            _emit_cascade_cancellations(logs_dir, cascaded, parent_id, parent_state)
        if requeued:
            _wake_dispatchers()
        return {"ok": True}

    @app.get("/workers", response_model=list[WorkerInfo])
    def list_workers():
        with SessionLocal() as session:
            workers = session.execute(select(Worker)).scalars().all()
            return [
                WorkerInfo(
                    host=w.host,
                    host_aliases=json.loads(w.host_aliases_json or "[]"),
                    last_heartbeat=w.last_heartbeat,
                    state=w.state,
                    free_vram_gb=w.free_vram_gb,
                    unregistered_vram_gb=w.unregistered_vram_gb,
                    free_ram_gb=w.free_ram_gb,
                    idle_cpus=w.idle_cpus,
                    arch=w.arch,
                    os=w.os,
                    gpu=w.gpu,
                    tags=json.loads(w.tags_json or "[]"),
                    mount_roots=json.loads(w.mount_roots_json or "[]"),
                    max_concurrent=w.max_concurrent if w.max_concurrent is not None else 1,
                    running=w.running if w.running is not None else 0,
                )
                for w in workers
            ]

    @app.delete("/workers/{host}")
    def delete_worker(host: str):
        with SessionLocal() as session:
            worker = session.execute(select(Worker).where(Worker.host == host)).scalar_one_or_none()
            if worker is None:
                raise HTTPException(status_code=404, detail=f"no such worker: {host}")
            if worker.state == "online":
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"worker {host!r} is online — stop the worker process first, "
                        "or wait for the sweeper to mark it offline"
                    ),
                )
            session.delete(worker)
            session.commit()
            return {"ok": True, "deleted": host}

    @app.post("/next-job", response_model=JobInfo | None)
    def next_job(q: NextJobQuery):
        from jobd.matcher import pick_next_job

        def _attempt() -> JobInfo | None:
            """One claim attempt against the live queue. Returns the claimed job
            or None (no match OR lost race — both mean 'nothing for you now')."""
            with SessionLocal() as session:
                all_queued = (
                    session.execute(select(Job).where(Job.state == JobState.QUEUED)).scalars().all()
                )
                queued: list[Job] = [j for j in all_queued if _deps_satisfied(j, session)]
                worker_row = session.execute(
                    select(Worker).where(Worker.host == q.host)
                ).scalar_one_or_none()
                aliases: list[str] = ["any", "any-gpu"] if q.free_vram_gb > 0 else ["any"]
                arch = q.arch
                os_ = q.os
                gpu = q.gpu or q.free_vram_gb > 0
                tags: list[str] = list(q.tags)
                mount_roots: list[str] = list(q.mount_roots)
                if worker_row is not None:
                    aliases = json.loads(worker_row.host_aliases_json)
                    arch = worker_row.arch
                    os_ = worker_row.os
                    gpu = worker_row.gpu
                    tags = json.loads(worker_row.tags_json)
                    mount_roots = json.loads(worker_row.mount_roots_json or "[]")
                # Filter out jobs whose cwd can't exist on this worker. Non-empty
                # mount_roots is the signal that the worker reported — older
                # workers that don't advertise roots get the old behavior (all
                # jobs eligible).
                if mount_roots:
                    queued = [j for j in queued if any(j.cwd.startswith(r) for r in mount_roots)]
                w = WorkerSnapshot(
                    host=q.host,
                    host_aliases=aliases,
                    free_vram_gb=q.free_vram_gb,
                    unregistered_vram_gb=q.unregistered_vram_gb,
                    free_ram_gb=q.free_ram_gb,
                    idle_cpus=q.idle_cpus,
                    arch=arch,
                    os=os_,
                    gpu=gpu,
                    tags=tags,
                )
                pick = pick_next_job(queued, w)
                if pick is None:
                    from jobd.matcher import explain_skip

                    skips = explain_skip(queued, w)
                    skip_state: dict[int, str] = state["dispatch_skip_state"]
                    queued_by_id = {j.id: j for j in queued}
                    for job_id, reason in skips:
                        if skip_state.get(job_id) != reason:
                            skip_state[job_id] = reason
                            _emit_event(
                                logs_dir,
                                "dispatch_skip",
                                source="broker",
                                job_id=job_id,
                                project=queued_by_id[job_id].project,
                                worker=q.host,
                                reason=reason,
                            )
                    # Drop dedup entries for jobs no longer in the queue (so a
                    # job that ran or was cancelled doesn't leak memory, and a
                    # resubmit gets a fresh slot).
                    for stale in [k for k in skip_state if k not in queued_by_id]:
                        del skip_state[stale]
                    return None
                # Atomic claim: only one worker can transition queued -> assigned
                result = session.execute(
                    Job.__table__.update()  # type: ignore[attr-defined]  # SQLAlchemy: Table.update on __table__
                    .where(Job.id == pick.id, Job.state == JobState.QUEUED)
                    .values(state=JobState.ASSIGNED, worker=q.host, started_at=datetime.now(UTC))
                )
                session.commit()
                if result.rowcount == 0:  # type: ignore[attr-defined]  # SQLAlchemy: CursorResult.rowcount
                    return None  # lost race; re-attempt / re-wait
                session.refresh(pick)
                # SQLite stores naive UTC; mirror the sweeper pattern (line ~999)
                # so the subtract is naive-to-naive.
                queue_wait_s = (
                    datetime.now(UTC).replace(tzinfo=None) - pick.submitted_at
                ).total_seconds()
                _emit_event(
                    logs_dir,
                    "job_dispatched",
                    source="broker",
                    job_id=pick.id,
                    project=pick.project,
                    worker=q.host,
                    queue_wait_s=round(queue_wait_s, 2),
                )
                return _to_info(pick)

        # Long-poll: hold the request up to q.wait_s, re-attempting whenever a
        # dispatcher wake fires (or every _LONGPOLL_RECHECK_S as a backstop for
        # any wake site we miss). wait_s=0 → single attempt, legacy behavior.
        wait_s = max(0.0, float(q.wait_s or 0.0))
        deadline = time.monotonic() + wait_s
        while True:
            info = _attempt()
            if info is not None:
                return info
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return None
            with _job_wake:
                _job_wake.wait(timeout=min(remaining, _LONGPOLL_RECHECK_S))

    @app.post("/events", status_code=204)
    def ingest_event(body: EventIngest):
        """Generic audit-event ingester for non-broker sources.

        Source is allowlisted to {worker, hook, mcp} by the Pydantic Literal —
        `source="broker"` yields 422, preserving the single-writer property for
        broker-emitted events. Broker stamps `ts` server-side via _emit_event.
        """
        _emit_event(
            logs_dir,
            body.event,
            source=body.source,
            job_id=body.job_id,
            project=body.project,
            **body.payload,
        )
        return Response(status_code=204)

    @app.get("/events")
    def get_events(
        since: str | None = None,
        project: str | None = None,
        event: str | None = None,
        job_id: int | None = None,
        source: str | None = None,
        limit: int = 1000,
    ):
        """Return filtered event-stream rows from events.jsonl (newest-last).

        Filters: since (relative `Nh|Nd|Nw` or ISO-8601), project, event,
        job_id, source. limit defaults to 1000, clamped to [1, 10000].

        Rows missing the `source` field (legacy pre-schema-v2) are skipped
        silently with a per-request INFO log. Malformed JSON lines and rows
        with unparseable `ts` are skipped defensively.
        """
        limit = max(1, min(10000, int(limit)))
        cutoff = _parse_since(since) if since else None

        def _match(row: dict) -> bool:
            # Rows missing `source` are legacy (pre-schema-v2) — excluded, same
            # as before. The reverse-reader handles the ts/cutoff early-stop.
            if "source" not in row:
                return False
            if project is not None and row.get("project") != project:
                return False
            if event is not None and row.get("event") != event:
                return False
            if job_id is not None and row.get("job_id") != job_id:
                return False
            if source is not None and row.get("source") != source:  # noqa: SIM103 — parallel guard-clause filter
                return False
            return True

        return _events.read_events(logs_dir, match=_match, cutoff=cutoff, limit=limit)

    @app.get("/projects")
    def list_projects():
        return _projects_to_jsonable(state["projects"])

    @app.post("/projects/{name}")
    def set_project_priority(name: str, payload: dict):
        priority = max(0, min(100, int(payload["priority"])))
        existing = state["projects"].get(name)
        if existing is None:
            state["projects"][name] = ProjectEntry(priority=priority)
        else:
            existing.priority = priority
        _persist_projects(state)
        return _projects_to_jsonable(state["projects"])

    @app.post("/projects/{name}/nudge")
    def nudge_project_priority(name: str, payload: dict):
        delta = int(payload["delta"])
        existing = state["projects"].get(name)
        if existing is None:
            base_entry = state["projects"].get("_default")
            base = base_entry.priority if base_entry is not None else 40
            new_priority = max(0, min(100, base + delta))
            state["projects"][name] = ProjectEntry(priority=new_priority)
        else:
            existing.priority = max(0, min(100, existing.priority + delta))
        _persist_projects(state)
        return _projects_to_jsonable(state["projects"])

    @app.post("/reload")
    def reload_config():
        state["projects"] = load_projects(state["paths"]["projects"])
        state["profiles"] = load_profiles(state["paths"]["profiles"])
        state["classifier"] = load_classifier_rules(state["paths"]["classifier"])
        return {"reloaded": True}

    @app.post("/resolve", response_model=ResolvedConfig)
    def resolve_job(req: JobSubmit) -> ResolvedConfig:
        """Dry-run submit: return the effective resolved config without
        enqueuing a job. Sources are tagged so callers can see *why* each
        field landed where it did.
        """
        profile_spec = None
        if req.profile:
            profile_spec = resolve_profile(state["profiles"], req.profile)
            if profile_spec is None:
                raise HTTPException(status_code=404, detail=f"unknown profile: {req.profile}")

        proj_defaults = resolve_project_defaults(state["projects"], req.project)

        # priority: existing logic (project base + delta).
        priority_value = resolve_priority(state["projects"], req.project, req.priority_delta)
        priority_source: ResolutionSource = "cli" if req.priority_delta != 0 else "project_default"
        if req.project not in state["projects"]:
            priority_source = "global"

        # host_pin
        host_pin_source: ResolutionSource
        if req.host_pin != "any":
            host_pin_value = req.host_pin
            host_pin_source = "cli"
        elif proj_defaults.host_pin:
            host_pin_value = proj_defaults.host_pin
            host_pin_source = "project_default"
        elif profile_spec and profile_spec.host_hint and profile_spec.host_hint != "any":
            host_pin_value = profile_spec.host_hint
            host_pin_source = "profile"
        else:
            host_pin_value = "any"
            host_pin_source = "global"

        # max_wall_s / idle_timeout_s — no profile-level default; fall through
        # CLI -> project -> global(None).
        max_wall_source: ResolutionSource
        if req.max_wall_s is not None:
            max_wall_value = req.max_wall_s
            max_wall_source = "cli"
        elif proj_defaults.max_wall_s is not None:
            max_wall_value = proj_defaults.max_wall_s
            max_wall_source = "project_default"
        else:
            max_wall_value = None
            max_wall_source = "global"

        idle_source: ResolutionSource
        if req.idle_timeout_s is not None:
            idle_value = req.idle_timeout_s
            idle_source = "cli"
        elif proj_defaults.idle_timeout_s is not None:
            idle_value = proj_defaults.idle_timeout_s
            idle_source = "project_default"
        else:
            idle_value = None
            idle_source = "global"

        ckpt_source: ResolutionSource
        if req.checkpoint_grace_s is not None:
            ckpt_value = req.checkpoint_grace_s
            ckpt_source = "cli"
        elif proj_defaults.checkpoint_grace_s is not None:
            ckpt_value = proj_defaults.checkpoint_grace_s
            ckpt_source = "project_default"
        else:
            ckpt_value = None
            ckpt_source = "global"

        # preemptible
        pre_source: ResolutionSource
        if req.preemptible is not None:
            pre_value = req.preemptible
            pre_source = "cli"
        elif proj_defaults.preemptible is not None:
            pre_value = proj_defaults.preemptible
            pre_source = "project_default"
        elif profile_spec is not None and profile_spec.preemptible:
            pre_value = profile_spec.preemptible
            pre_source = "profile"
        else:
            pre_value = False
            pre_source = "global"

        # requires
        req_source: ResolutionSource
        if req.requires is not None:
            req_value = req.requires.model_dump()
            req_source = "cli"
        elif proj_defaults.requires is not None:
            req_value = proj_defaults.requires.model_dump()
            req_source = "project_default"
        elif profile_spec is not None and profile_spec.requires is not None:
            req_value = profile_spec.requires.model_dump()
            req_source = "profile"
        else:
            req_value = None
            req_source = "global"

        # escalate_to_arc — only project-default or global today.
        arc_source: ResolutionSource
        if proj_defaults.escalate_to_arc:
            arc_value = True
            arc_source = "project_default"
        else:
            arc_value = False
            arc_source = "global"

        warning: str | None = None
        if req.project not in state["projects"] and "_default" in state["projects"]:
            warning = (
                f"project {req.project!r} has no entry in projects.yaml; using global defaults"
            )

        return ResolvedConfig(
            project=req.project,
            effective_priority=FieldResolution(value=priority_value, source=priority_source),
            effective_host_pin=FieldResolution(value=host_pin_value, source=host_pin_source),
            effective_max_wall_s=FieldResolution(value=max_wall_value, source=max_wall_source),
            effective_idle_timeout_s=FieldResolution(value=idle_value, source=idle_source),
            effective_checkpoint_grace_s=FieldResolution(value=ckpt_value, source=ckpt_source),
            effective_preemptible=FieldResolution(value=pre_value, source=pre_source),
            effective_requires=FieldResolution(value=req_value, source=req_source),
            effective_escalate_to_arc=FieldResolution(value=arc_value, source=arc_source),
            submit_warning=warning,
        )

    def _prune_old_jobs(now: datetime | None = None) -> int:
        """Delete terminal jobs and their per-job .log files whose ``finished_at``
        is older than ``JOBD_JOB_RETENTION_DAYS``. Returns the count pruned; 0
        days (the default) disables pruning entirely — opt-in so an existing
        deployment never silently loses history.

        Keeps the jobs table and log dir bounded by the retention window on a
        long-running broker (events.jsonl is bounded separately by size
        rotation). Freed SQLite pages are reused under WAL, so the DB file stays
        bounded without a global-locking VACUUM. Removing old terminal rows is
        safe for any still-pending dependents: ``_deps_satisfied`` already treats
        a pruned parent as satisfied.
        """
        raw = os.environ.get("JOBD_JOB_RETENTION_DAYS", "").strip()
        try:
            retention_days = int(raw) if raw else JOB_RETENTION_DAYS_DEFAULT
        except ValueError:
            retention_days = JOB_RETENTION_DAYS_DEFAULT
        if retention_days <= 0:
            return 0
        if now is None:
            now = datetime.now(UTC).replace(tzinfo=None)
        cutoff = now - timedelta(days=retention_days)
        terminal = [s.value for s in TERMINAL_STATES]
        with SessionLocal() as session:
            pruned_ids = list(
                session.execute(
                    select(Job.id).where(
                        Job.state.in_(terminal),
                        Job.finished_at.is_not(None),
                        Job.finished_at < cutoff,
                    )
                ).scalars()
            )
            if not pruned_ids:
                return 0
            session.execute(Job.__table__.delete().where(Job.id.in_(pruned_ids)))  # type: ignore[attr-defined]  # SQLAlchemy: Table.delete on __table__
            session.commit()
        # Unlink each per-job .log AFTER the rows are gone: a leftover log for a
        # deleted row is harmless disk, whereas deleting the log of a row that
        # survived (had the delete failed) would 404 a live `job output`.
        for jid in pruned_ids:
            try:
                (logs_dir / f"{jid}.log").unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                log.warning("retention: could not unlink %s.log: %s", jid, e)
        _emit_event(
            logs_dir,
            "jobs_pruned",
            source="broker",
            count=len(pruned_ids),
            retention_days=retention_days,
        )
        return len(pruned_ids)

    def _sweep_once():
        """One pass: reclaim orphans, mark offline workers."""
        # SQLite stores datetimes as naive UTC; compare naive-to-naive.
        now = datetime.now(UTC).replace(tzinfo=None)
        going_offline_records: list[tuple[str, datetime | None]] = []
        scheduling_timeout_records: list[tuple[int, str, int]] = []
        with SessionLocal() as session:
            # Audit 2026-05-18 (runtime-zombies S3): expire queued jobs whose
            # caller-supplied scheduling_timeout_s elapsed. Hatchet pattern,
            # motivated by jobs 577/578 on server. Done FIRST so subsequent
            # sweeper passes (unmatcheable warning, blocker probe) don't
            # waste work on already-terminal rows.
            timeout_candidates = (
                session.execute(
                    select(Job).where(
                        Job.state == JobState.QUEUED,
                        Job.scheduling_timeout_s.is_not(None),
                    )
                )
                .scalars()
                .all()
            )
            for j in timeout_candidates:
                deadline_age_s = (now - j.submitted_at).total_seconds()
                if deadline_age_s <= j.scheduling_timeout_s:
                    continue
                j.state = JobState.SCHEDULING_TIMEOUT.value
                j.finished_at = now
                j.termination_reason = "scheduling_timeout"
                j.signal = None
                # Audit 2026-05-18 spec-review (S3 fix): no cascade on
                # scheduling-timeout. Spec called only for the queued-job
                # auto-cancel; cascading silently failed dependents of jobs
                # that never even dispatched — out of scope. Real failures
                # (jobs that ran and crashed) still cascade via the worker
                # /complete + worker-reclaim paths above/below.
                scheduling_timeout_records.append((j.id, j.project, int(j.scheduling_timeout_s)))
            # Mark workers offline past threshold (split SELECT-then-set so we
            # can emit one worker_offline event per transitioning worker).
            offline_cutoff = now - timedelta(seconds=OFFLINE_AFTER_SECONDS)
            going_offline = (
                session.execute(
                    select(Worker).where(
                        Worker.last_heartbeat < offline_cutoff,
                        Worker.state != "offline",
                    )
                )
                .scalars()
                .all()
            )
            for w in going_offline:
                w.state = "offline"
                # Capture into local Python values BEFORE commit to avoid
                # SQLA detached-instance issues when emitting after commit.
                going_offline_records.append((w.host, w.last_heartbeat))

            # Audit 2026-05-18 (runtime-zombies S6): stale-worker transition.
            # Workers silent for >threshold but <OFFLINE_AFTER get marked
            # `stale` so the matcher (which only sees state=='online' rows)
            # stops dispatching to them. The longer offline transition
            # above still wins for fully-dead workers. Configurable via
            # JOBD_STALE_WORKER_THRESHOLD_S.
            try:
                stale_threshold_s = int(
                    os.environ.get(
                        "JOBD_STALE_WORKER_THRESHOLD_S",
                        STALE_WORKER_THRESHOLD_S_DEFAULT,
                    )
                )
            except ValueError:
                stale_threshold_s = STALE_WORKER_THRESHOLD_S_DEFAULT
            stale_cutoff = now - timedelta(seconds=stale_threshold_s)
            going_stale = (
                session.execute(
                    select(Worker).where(
                        Worker.last_heartbeat < stale_cutoff,
                        Worker.state == "online",
                    )
                )
                .scalars()
                .all()
            )
            going_stale_records: list[tuple[str, datetime | None]] = []
            for w in going_stale:
                w.state = "stale"
                going_stale_records.append((w.host, w.last_heartbeat))

            # Reclaim assigned jobs whose worker is stale
            assigned = (
                session.execute(select(Job).where(Job.state == JobState.ASSIGNED)).scalars().all()
            )
            for j in assigned:
                reclaim_seconds = DEAD_WORKER_SECONDS
                if j.requires_json and j.requires_json != "{}":
                    try:
                        req = JobRequires.model_validate_json(j.requires_json)
                        if req.idempotent:
                            reclaim_seconds = IDEMPOTENT_RECLAIM_SECONDS
                    except Exception:
                        pass
                cutoff = now - timedelta(seconds=reclaim_seconds)
                w = session.execute(
                    select(Worker).where(Worker.host == j.worker)
                ).scalar_one_or_none()
                if w is None or w.last_heartbeat < cutoff:
                    j.state = JobState.QUEUED
                    j.worker = None
                    j.started_at = None

            # Pre-launch CRIT-2: reclaim RUNNING jobs whose worker has died.
            # A worker crash mid-run otherwise strands the job in RUNNING
            # forever — no /complete ever lands, dependents never unblock, and
            # /wait never returns. Idempotent jobs are safe to re-run, so we
            # requeue them like the ASSIGNED path above; non-idempotent jobs
            # already had side effects, so we transition them to ORPHANED (a
            # failed-side terminal state that cascades to dependents) rather
            # than silently re-running a job that may not be re-runnable.
            orphan_records: list[tuple[int, str, str | None, datetime | None, str]] = []
            cascade_records: list[tuple[int, str, list[tuple[int, str]]]] = []
            running = (
                session.execute(select(Job).where(Job.state == JobState.RUNNING)).scalars().all()
            )
            for j in running:
                # Wall-clock backstop (independent of worker liveness). Worker-
                # side enforcement (job_worker.poll_signals SIGTERMs at
                # max_wall_s) lives only in the worker's per-job monitor; if the
                # worker crashed mid-run and restarted with no memory of the
                # job, that enforcement never lands and no /complete is ever
                # posted, so the job is stranded RUNNING forever even though the
                # worker is heartbeating again. A healthy worker would have
                # terminated + reported within seconds of max_wall_s, far inside
                # the grace, so this never races one. wall_clock_exceeded is a
                # terminal "ran too long" condition, so orphan regardless of
                # idempotency (re-running would just blow the same wall clock).
                # Regression: stranded job 1364 (desktop host-power crash 2026-06).
                if j.max_wall_s is not None and j.started_at is not None:
                    grace = WALL_CLOCK_BACKSTOP_GRACE_SECONDS + (j.checkpoint_grace_s or 0)
                    if (now - j.started_at).total_seconds() > j.max_wall_s + grace:
                        last_hb = None
                        if j.worker is not None:
                            wk = session.execute(
                                select(Worker).where(Worker.host == j.worker)
                            ).scalar_one_or_none()
                            last_hb = wk.last_heartbeat if wk is not None else None
                        j.state = JobState.ORPHANED.value
                        j.finished_at = now
                        j.termination_reason = "wall_clock_exceeded"
                        j.signal = None
                        cascaded = _cascade_on_parent_terminal(session, j, logs_dir)
                        cascade_records.append((j.id, j.state, cascaded))
                        orphan_records.append(
                            (j.id, j.project, j.worker, last_hb, "wall_clock_exceeded")
                        )
                        continue
                reclaim_seconds = DEAD_WORKER_SECONDS
                idempotent = False
                if j.requires_json and j.requires_json != "{}":
                    try:
                        req = JobRequires.model_validate_json(j.requires_json)
                        if req.idempotent:
                            idempotent = True
                            reclaim_seconds = IDEMPOTENT_RECLAIM_SECONDS
                    except Exception:
                        pass
                cutoff = now - timedelta(seconds=reclaim_seconds)
                w = session.execute(
                    select(Worker).where(Worker.host == j.worker)
                ).scalar_one_or_none()
                if w is not None and w.last_heartbeat >= cutoff:
                    continue  # worker still alive — leave the job running
                last_hb = w.last_heartbeat if w is not None else None
                if idempotent:
                    j.state = JobState.QUEUED
                    j.worker = None
                    j.started_at = None
                else:
                    # Set the string value (not the enum) so the cascade's
                    # `parent.state in {...value}` membership test fires.
                    j.state = JobState.ORPHANED.value
                    j.finished_at = now
                    j.termination_reason = "worker_died"
                    j.signal = None
                    cascaded = _cascade_on_parent_terminal(session, j, logs_dir)
                    cascade_records.append((j.id, j.state, cascaded))
                    orphan_records.append((j.id, j.project, j.worker, last_hb, "worker_died"))

            session.commit()
            # Reclaims/requeues and orphan cascades above may have made jobs
            # dispatchable — wake any long-polling workers.
            _wake_dispatchers()
            for jid, proj, wkr, last_hb, reason in orphan_records:
                _emit_event(
                    logs_dir,
                    "job_orphaned",
                    source="broker",
                    job_id=jid,
                    project=proj,
                    worker=wkr,
                    last_heartbeat=last_hb.isoformat() if last_hb else None,
                    termination_reason=reason,
                )
            for parent_id, parent_state, cascaded in cascade_records:
                _emit_cascade_cancellations(logs_dir, cascaded, parent_id, parent_state)
            for host, last_hb in going_offline_records:
                _emit_event(
                    logs_dir,
                    "worker_offline",
                    source="broker",
                    host=host,
                    last_heartbeat=last_hb.isoformat() if last_hb else None,
                )
            # Audit 2026-05-18 (runtime-zombies S6): worker_stale event so
            # the operator can see the matcher-eligibility flip in the
            # broker event log alongside the existing worker_offline emit.
            for host, last_hb in going_stale_records:
                _emit_event(
                    logs_dir,
                    "worker_stale",
                    source="broker",
                    host=host,
                    last_heartbeat=last_hb.isoformat() if last_hb else None,
                )
            for job_id, project, threshold_s in scheduling_timeout_records:
                _emit_event(
                    logs_dir,
                    "scheduling_timeout",
                    source="broker",
                    job_id=job_id,
                    project=project,
                    threshold_s=threshold_s,
                )

            # Soft unmatcheable warning: queued >60s + no online worker advertises caps
            stale_queued = (
                session.execute(
                    select(Job).where(
                        Job.state == JobState.QUEUED,
                        Job.submitted_at < now - timedelta(seconds=UNMATCHEABLE_THRESHOLD_SECONDS),
                    )
                )
                .scalars()
                .all()
            )
            workers = (
                session.execute(select(Worker).where(Worker.state == "online")).scalars().all()
            )
            snapshots = _build_snapshots(list(workers))
            host_list = [w.host for w in workers]
            for j in stale_queued:
                req = None
                if j.requires_json and j.requires_json != "{}":
                    try:
                        req = JobRequires.model_validate_json(j.requires_json)
                    except Exception:
                        continue
                shim = SimpleNamespace(
                    id=j.id,
                    priority=j.priority,
                    submitted_at=j.submitted_at,
                    host_pin=j.host_pin,
                    vram_gb=j.vram_gb,
                    ram_gb=j.ram_gb,
                    cpus=j.cpus,
                    requires=req,
                )
                # req=None means job matches any worker by definition — an empty
                # fleet is a queueing delay, not a capability mismatch.
                matcheable = req is None or any(selectors_only_match(shim, ws) for ws in snapshots)

                blocker_warning: str | None = None
                if matcheable:
                    age_s = (now - j.submitted_at).total_seconds()
                    if age_s >= QUEUE_BLOCKED_THRESHOLD_SECONDS:
                        elig = eligible_workers(req, j.host_pin, snapshots)
                        if elig:
                            candidate = _find_preemptible_candidate(elig, j, session, now)
                            if candidate is not None:
                                candidate.signal = "preempt"
                                candidate.warning = f"{_AUTO_PREEMPT_WARNING_PREFIX}{j.id}"
                                candidate.warning_at = now
                                _emit_event(
                                    logs_dir,
                                    "auto_preempt",
                                    source="broker",
                                    job_id=candidate.id,
                                    project=candidate.project,
                                    cause="sweeper",
                                    queued_job=j.id,
                                    worker=candidate.worker,
                                    queued_priority=j.priority,
                                    candidate_priority=candidate.priority,
                                    queue_age_s=int(age_s),
                                )
                            else:
                                blocker = _find_nonpreemptible_blocker(elig, session)
                                if blocker is not None:
                                    blocker_warning = (
                                        f"{_BLOCKED_WARNING_PREFIX}{int(age_s // 60)}m: "
                                        f"blocked by non-preemptible job {blocker.id} on "
                                        f"{blocker.worker}; preempt with `job preempt {blocker.id}`"
                                    )

                if not matcheable:
                    new_w = (
                        f"{_UNMATCHEABLE_WARNING_PREFIX} none of {host_list} "
                        f"advertise required capabilities"
                    )
                    if j.warning != new_w:
                        j.warning = new_w
                        j.warning_at = now
                        _emit_event(
                            logs_dir,
                            "sweep_warning",
                            source="broker",
                            job_id=j.id,
                            project=j.project,
                            warning_text=j.warning,
                        )
                elif blocker_warning is not None:
                    if j.warning != blocker_warning:
                        j.warning = blocker_warning
                        j.warning_at = now
                        _emit_event(
                            logs_dir,
                            "sweep_warning",
                            source="broker",
                            job_id=j.id,
                            project=j.project,
                            warning_text=j.warning,
                        )
                elif j.warning and (
                    j.warning.startswith(_UNMATCHEABLE_WARNING_PREFIX)
                    or j.warning.startswith(_BLOCKED_WARNING_PREFIX)
                ):
                    # State improved — clear stale unmatcheable/blocked warnings.
                    # "will queue behind" warnings set at submit are preserved
                    # since they describe a different (predictive) signal.
                    j.warning = None
                    j.warning_at = None
            session.commit()

        # Retention prune (own session, opt-in via JOBD_JOB_RETENTION_DAYS).
        _prune_old_jobs(now)

    # Expose as test seams at module scope
    globals()["_sweep_once"] = _sweep_once
    globals()["_prune_old_jobs"] = _prune_old_jobs
    globals()["_engine_for_testing"] = lambda: engine

    return app


def _entry_to_yaml_dict(entry: ProjectEntry) -> dict:
    """Serialize one ProjectEntry to the YAML on-disk shape.

    Critical: emit the ``defaults:`` block whenever it carries non-zero
    values. Older code dropped defaults on every ``set_project_priority``
    or ``nudge_project_priority`` call, silently erasing them after one
    nudge — see docs/projects-yaml.md §8 round-trip canary test.
    """
    out: dict = {"priority": entry.priority}
    d = entry.defaults
    defaults_dict: dict = {}
    if d.max_wall_s is not None:
        defaults_dict["max_wall_s"] = d.max_wall_s
    if d.idle_timeout_s is not None:
        defaults_dict["idle_timeout_s"] = d.idle_timeout_s
    if d.checkpoint_grace_s is not None:
        defaults_dict["checkpoint_grace_s"] = d.checkpoint_grace_s
    if d.host_pin is not None:
        defaults_dict["host_pin"] = d.host_pin
    if d.requires is not None:
        defaults_dict["requires"] = d.requires.model_dump(exclude_none=False)
    if d.preemptible is not None:
        defaults_dict["preemptible"] = d.preemptible
    if d.priority is not None:
        defaults_dict["priority"] = d.priority
    if d.escalate_to_arc:
        defaults_dict["escalate_to_arc"] = d.escalate_to_arc
    if defaults_dict:
        out["defaults"] = defaults_dict
    return out


def _entry_to_jsonable(entry: ProjectEntry) -> dict:
    """Serialize a ProjectEntry to a JSON-safe shape for HTTP responses."""
    return _entry_to_yaml_dict(entry)


def _projects_to_jsonable(projects: dict[str, ProjectEntry]) -> dict[str, dict]:
    return {name: _entry_to_jsonable(entry) for name, entry in projects.items()}


def _persist_projects(state: BrokerState) -> None:
    """Write the in-memory projects dict back to YAML in the canonical shape.

    Round-trip safety: must preserve ``defaults:`` blocks exactly so that a
    single ``job projects nudge`` does not silently erase per-project
    overrides. See test_projects_yaml.test_persist_projects_round_trip.
    """
    data = {
        "projects": {name: _entry_to_yaml_dict(entry) for name, entry in state["projects"].items()}
    }
    state["paths"]["projects"].write_text(yaml.safe_dump(data, sort_keys=False))


_DEPENDS_TERMINAL = {JobState.COMPLETED.value}
_DEPENDS_TERMINAL_ANY = {
    JobState.COMPLETED.value,
    JobState.FAILED.value,
    JobState.CANCELLED.value,
    JobState.PREEMPTED.value,
    JobState.ORPHANED.value,
    JobState.SCHEDULING_TIMEOUT.value,
}


def _deps_satisfied(job: Job, session) -> bool:
    """True iff job's depends_on list is empty OR every parent has reached a
    terminal state that matches the child's policy.

    Policy:
      - depends_on_any_exit=False (default): parents must reach COMPLETED.
        If any parent is FAILED/CANCELLED/ORPHANED/PREEMPTED/SCHEDULING_TIMEOUT,
        the child will be cascade-cancelled elsewhere — return False here so the
        matcher doesn't dispatch it while the cascade is still pending.
      - depends_on_any_exit=True: any terminal state unblocks.
    """
    deps = json.loads(job.depends_on_json or "[]")
    if not deps:
        return True
    terminal = _DEPENDS_TERMINAL_ANY if job.depends_on_any_exit else _DEPENDS_TERMINAL
    for dep_id in deps:
        parent = session.get(Job, dep_id)
        if parent is None:
            # Pruned parent — treat as satisfied (generous default).
            continue
        if parent.state not in terminal:
            return False
    return True


def _cascade_on_parent_terminal(session, parent: Job, logs_dir: Path) -> list[tuple[int, str]]:
    """When parent reaches a failed-side terminal state, cancel children that
    did not opt into depends_on_any_exit. Returns (child_id, project) for each
    cancelled child so the caller can emit `job_cancelled` events with
    `by='cascade'` (schema-v2) AFTER its commit."""
    if parent.state not in {
        JobState.FAILED.value,
        JobState.CANCELLED.value,
        JobState.ORPHANED.value,
        JobState.PREEMPTED.value,
        JobState.SCHEDULING_TIMEOUT.value,
    }:
        return []
    # SQLite stores datetimes as naive UTC; keep finished_at/warning_at naive.
    now = datetime.now(UTC).replace(tzinfo=None)
    cancelled: list[tuple[int, str]] = []
    candidates = (
        session.execute(select(Job).where(Job.state == JobState.QUEUED.value)).scalars().all()
    )
    for child in candidates:
        if child.depends_on_any_exit:
            continue
        deps = json.loads(child.depends_on_json or "[]")
        if parent.id not in deps:
            continue
        child.state = JobState.CANCELLED.value
        child.finished_at = now
        child.warning = f"parent_failed: {parent.id} → {parent.state}"
        child.warning_at = now
        cancelled.append((child.id, child.project))
    return cancelled


def _emit_cascade_cancellations(
    logs_dir: Path,
    cancelled: list[tuple[int, str]],
    parent_id: int,
    parent_state: str,
) -> None:
    """Emit one `job_cancelled` event (by='cascade') per child cancelled by
    `_cascade_on_parent_terminal`. Call this AFTER the caller's commit so a
    failed commit can't leave events.jsonl claiming a cancel the DB never
    recorded."""
    for child_id, child_project in cancelled:
        _emit_event(
            logs_dir,
            "job_cancelled",
            source="broker",
            job_id=child_id,
            project=child_project,
            prior_state=JobState.QUEUED.value,
            by="cascade",
            parent_job=parent_id,
            parent_state=parent_state,
        )


def _reconcile_worker_in_flight(
    session,
    host: str,
    reported: set[int],
    logs_dir: Path,
) -> tuple[list[tuple[int, str]], list[tuple[int, str, list[tuple[int, str]]]], list[int]]:
    """SIGTERM-drain Phase 2: reconcile a worker's reported in-flight set
    against broker-side claims (docs/plans/sigterm-drain.md).

    A worker that died without draining (SIGKILL, crash, power loss) restarts
    within seconds under Restart=on-failure and heartbeats again — refreshing
    the liveness clock the dead-worker reaper keys on — while knowing nothing
    about the jobs it was running. Those jobs would strand in RUNNING/ASSIGNED
    forever. Any job claimed by `host`, older than RECONCILE_MIN_AGE_SECONDS
    since its claim, and missing from RECONCILE_MISS_THRESHOLD consecutive
    reports gets the worker-died disposition: ASSIGNED jobs requeue (the
    workload never started, so no side effects — this also recovers a lost
    /next-job response, which the heartbeat-keyed reaper never catches);
    idempotent RUNNING jobs requeue; non-idempotent RUNNING jobs go ORPHANED
    with termination_reason="worker_restarted" and cascade to dependents.

    Mutates job rows only — the caller commits, then emits the returned
    (orphan_records, cascade_records, requeued_ids).
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    orphan_records: list[tuple[int, str]] = []
    cascade_records: list[tuple[int, str, list[tuple[int, str]]]] = []
    requeued: list[int] = []
    claimed = (
        session.execute(
            select(Job).where(
                Job.state.in_([JobState.ASSIGNED.value, JobState.RUNNING.value]),
                Job.worker == host,
            )
        )
        .scalars()
        .all()
    )
    for j in claimed:
        if j.id in reported:
            if j.reconcile_misses:
                j.reconcile_misses = 0
            continue
        if j.started_at is None or (now - j.started_at).total_seconds() < RECONCILE_MIN_AGE_SECONDS:
            continue
        j.reconcile_misses = (j.reconcile_misses or 0) + 1
        if j.reconcile_misses < RECONCILE_MISS_THRESHOLD:
            continue
        idempotent = False
        if j.requires_json and j.requires_json != "{}":
            try:
                req = JobRequires.model_validate_json(j.requires_json)
                if req.idempotent:
                    idempotent = True
            except Exception:
                pass
        if j.state == JobState.ASSIGNED.value or idempotent:
            j.state = JobState.QUEUED.value
            j.worker = None
            j.started_at = None
            j.reconcile_misses = 0
            requeued.append(j.id)
        else:
            # String value (not the enum) so the cascade's membership test fires.
            j.state = JobState.ORPHANED.value
            j.finished_at = now
            j.termination_reason = "worker_restarted"
            j.signal = None
            cascaded = _cascade_on_parent_terminal(session, j, logs_dir)
            cascade_records.append((j.id, j.state, cascaded))
            orphan_records.append((j.id, j.project))
    return orphan_records, cascade_records, requeued


def _find_preemptible_candidate(
    elig: list[WorkerSnapshot],
    queued: Job,
    session,
    now: datetime,
) -> Job | None:
    """Find a preemptible job to displace in favor of a higher-priority queued
    job. Searches running/assigned jobs on eligible workers with priority
    strictly less than the queued job's priority, runtime above the
    AUTO_PREEMPT_MIN_RUNTIME_SECONDS floor, and no signal already set
    (don't double-fire).

    Returns the lowest-priority eligible candidate (most preemptable), or
    None if no candidate qualifies.
    """
    cutoff = now - timedelta(seconds=AUTO_PREEMPT_MIN_RUNTIME_SECONDS)
    hosts = [ws.host for ws in elig]
    if not hosts:
        return None
    return (
        session.execute(
            select(Job)
            .where(
                Job.worker.in_(hosts),
                Job.state.in_([JobState.ASSIGNED.value, JobState.RUNNING.value]),
                Job.preemptible.is_(True),
                Job.priority < queued.priority,
                Job.signal.is_(None),
                Job.started_at.is_not(None),
                Job.started_at <= cutoff,
            )
            .order_by(Job.priority.asc(), Job.started_at.asc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def _find_nonpreemptible_blocker(elig: list[WorkerSnapshot], session) -> Job | None:
    """If every eligible worker has a non-preemptible job in flight, return
    the first such blocker. Returns None if at least one eligible worker is
    free or running only preemptible jobs (the matcher can preempt those).
    """
    blocker: Job | None = None
    for ws in elig:
        running = (
            session.execute(
                select(Job).where(
                    Job.worker == ws.host,
                    Job.state.in_([JobState.ASSIGNED.value, JobState.RUNNING.value]),
                    Job.preemptible.is_(False),
                )
            )
            .scalars()
            .first()
        )
        if running is None:
            return None
        if blocker is None:
            blocker = running
    return blocker


def _serialization_warning(
    requires: JobRequires | None,
    host_pin: str,
    snapshots: list[WorkerSnapshot],
    session,
) -> str | None:
    """At submit time: if exactly one online worker can route this job AND
    that worker has a non-terminal job, return a 'will queue behind' string.
    Returns None otherwise.
    """
    elig = eligible_workers(requires, host_pin, snapshots)
    if len(elig) != 1:
        return None
    only = elig[0]
    busy = (
        session.execute(
            select(Job).where(
                Job.worker == only.host,
                Job.state.in_([JobState.ASSIGNED.value, JobState.RUNNING.value]),
            )
        )
        .scalars()
        .first()
    )
    if busy is None:
        return None
    return f"will queue behind job {busy.id} on {only.host}"


def _build_snapshots(workers: list[Worker]) -> list[WorkerSnapshot]:
    return [
        WorkerSnapshot(
            host=w.host,
            host_aliases=json.loads(w.host_aliases_json or "[]"),
            free_vram_gb=w.free_vram_gb,
            unregistered_vram_gb=w.unregistered_vram_gb,
            free_ram_gb=w.free_ram_gb,
            idle_cpus=w.idle_cpus,
            arch=w.arch,
            os=w.os,
            gpu=w.gpu,
            tags=json.loads(w.tags_json or "[]"),
            mount_roots=json.loads(w.mount_roots_json or "[]"),
        )
        for w in workers
    ]


def _to_info(job: Job, eta_ctx: dict | None = None) -> JobInfo:
    req = None
    if job.requires_json and job.requires_json != "{}":
        try:
            req = JobRequires.model_validate_json(job.requires_json)
        except Exception:
            req = None
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
        env=json.loads(job.env_json or "{}"),
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
