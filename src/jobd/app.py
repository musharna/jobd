"""FastAPI application factory for jobd.

Endpoints + wiring only: non-endpoint internals (state machine, sweeper,
scheduling, events, job-info shaping) live in the ``jobd.broker`` package —
see that package's docstring for the module map. Moving the endpoint closures
themselves onto APIRouters (Stage 3 of the 2026-07-02 split) is deliberately
parked: it's a ~90-reference rewrite of the live HTTP surface with zero
behavior change.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import logging
import os
import time
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import sessionmaker
from sse_starlette.sse import EventSourceResponse

from jobd import __version__
from jobd import events as _events
from jobd.auth import install_tailnet_acl, require_token
from jobd.broker.admission import refuse_admission as refuse_admission_service

# Broker internals split into jobd.broker.* (see that package's docstring). They
# are re-imported here so the endpoint closures in build_app() — and the tests
# that monkeypatch e.g. jobd.app._cas_state — resolve them from this module's
# namespace exactly as when they were defined inline.
from jobd.broker.constants import (
    _AUTO_PREEMPT_WARNING_PREFIX,
    _LONGPOLL_RECHECK_S,
    AUTO_PREEMPT_MIN_RUNTIME_SECONDS,  # noqa: F401  # re-exported: tests read jobd.app.AUTO_PREEMPT_MIN_RUNTIME_SECONDS
    LIST_LIMIT_MAX,
    MAX_LOG_CHUNK_BYTES,
    NON_TERMINAL_STATES,
    SWEEP_INTERVAL_SECONDS,
    WAIT_STREAM_CHUNK_BYTES,
)
from jobd.broker.context import BrokerState
from jobd.broker.events import _emit_event, _parse_since
from jobd.broker.jobinfo import _build_eta_ctx, _to_info
from jobd.broker.joblog import job_log_path
from jobd.broker.projects import (
    _persist_projects,
    _projects_to_jsonable,
)
from jobd.broker.scheduling import (
    _build_snapshots,
)
from jobd.broker.state import (
    _cas_state,
    _cascade_on_parent_terminal,
    _deps_satisfied_bulk,
    _emit_cascade_cancellations,
    _excluded_workers,
    _reconcile_worker_in_flight,
    _reject_stale_worker,
)
from jobd.broker.submit import submit_job
from jobd.broker.sweeper import prune_old_jobs, sweep_once
from jobd.config import (
    ProjectEntry,
    load_classifier_rules,
    load_effective_projects,
    load_profiles,
    resolve_effective_config,
    resolve_profile,
)
from jobd.db import Job, Worker, init_db, migrate
from jobd.matcher import (
    WorkerSnapshot,
    eligible_workers,
)
from jobd.metrics import build_metrics_app
from jobd.models import (
    TERMINAL_STATES,
    AdmissionRefusal,
    ClassifyRequest,
    ClassifyResult,
    CompletePayload,
    EventIngest,
    FieldResolution,
    JobInfo,
    JobRequires,
    JobState,
    JobSubmit,
    NextJobQuery,
    NudgePriorityRequest,
    ResolvedConfig,
    SetPriorityRequest,
    WorkerHeartbeat,
    WorkerInfo,
)

log = logging.getLogger("jobd")


def _state_dir_for(db_url: str, logs_dir: Path) -> Path:
    """Directory for MUTABLE broker state (the runtime priority overlay).

    Prefer $JOBD_STATE_DIR; else the directory of a file-backed SQLite DB (the
    ./data mount — writable by design, and already covered by the DB backup);
    else fall back to logs_dir (also writable). NEVER the config dir, which is
    git-owned and bind-mounted read-only — writing there is exactly the bug this
    split fixes (audit 2026-07-12).
    """
    env_dir = os.environ.get("JOBD_STATE_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    prefix = "sqlite:///"
    if db_url.startswith(prefix) and ":memory:" not in db_url:
        parent = Path(db_url[len(prefix) :]).parent
        if str(parent) not in ("", "."):
            return parent
    return logs_dir


def build_app(
    db_url: str,
    projects_path: Path | str,
    profiles_path: Path | str,
    classifier_path: Path | str,
    logs_path: Path | str | None = None,
    project_overrides_path: Path | str | None = None,
) -> FastAPI:
    # Long-poll plumbing (see _wake_dispatchers below). Defined up here so both
    # lifespan and the sweep can reach the loop handle.
    _loop_holder: list[asyncio.AbstractEventLoop | None] = [None]
    _wake_holder: list[asyncio.Event] = [asyncio.Event()]

    async def _sweep_loop():
        while True:
            try:
                # Run the (blocking, SQLite-touching) sweep off the event loop so
                # it can't freeze every async endpoint for the sweep's duration —
                # incl. the up-to-5s busy_timeout stalls (M5).
                await asyncio.to_thread(_sweep_once)
            except Exception as e:
                log.warning("sweeper error: %s", e)
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)

    @asynccontextmanager
    async def lifespan(app):
        _loop_holder[0] = asyncio.get_running_loop()
        task = asyncio.create_task(_sweep_loop())
        yield
        task.cancel()

    app = FastAPI(
        title="jobd",
        version=__version__,
        lifespan=lifespan,
        dependencies=[Depends(require_token)],
        # LOW-sec (audit 2026-07-10): the interactive docs + schema routes are
        # Starlette-mounted and bypass the app-level require_token dependency, so
        # a tokenless tailnet peer can enumerate the whole API surface at /docs,
        # /redoc, /openapi.json (live-proven 200 vs 401 on authed routes).
        # Disable them — jobd is a machine API, not a browsable service.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_tailnet_acl(app)

    # Connection pool (audit 2026-07-12): SQLAlchemy's QueuePool defaults to
    # pool_size=5 + max_overflow=10 = 15 connections — but TWO threadpools feed
    # this engine, and both open sessions: anyio's (40 tokens; every sync
    # endpoint) and asyncio.to_thread's executor (min(32, cpu+4); the /next-job
    # dispatch scan). Past 15 concurrent DB-touching requests the surplus threads
    # block on pool checkout for up to pool_timeout (30s) — a hard cliff reached
    # exactly when the dispatch fan-out wakes every parked worker at once. Size
    # the pool above both ceilings. (In-memory SQLite uses SingletonThreadPool,
    # which takes neither argument.)
    engine_kwargs: dict = {"future": True}
    if ":memory:" not in db_url:
        engine_kwargs["pool_size"] = int(os.environ.get("JOBD_DB_POOL_SIZE", "20"))
        engine_kwargs["max_overflow"] = int(os.environ.get("JOBD_DB_MAX_OVERFLOW", "60"))
    engine = create_engine(db_url, **engine_kwargs)

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

    # Config ownership split (audit 2026-07-12): projects.yaml is the git-owned,
    # read-only baseline; runtime priority changes land in a writable overlay
    # next to the DB. See jobd.config.load_effective_projects.
    overrides_path = (
        Path(project_overrides_path)
        if project_overrides_path is not None
        else _state_dir_for(db_url, logs_dir) / "project-priorities.yaml"
    )
    _projects, _base_priorities = load_effective_projects(projects_path, overrides_path)

    state: BrokerState = {
        "projects": _projects,
        "base_priorities": _base_priorities,
        "profiles": load_profiles(profiles_path),
        "classifier": load_classifier_rules(classifier_path),
        "paths": {
            "projects": Path(projects_path),
            "profiles": Path(profiles_path),
            "classifier": Path(classifier_path),
            "project_overrides": overrides_path,
        },
        "logs_dir": logs_dir,
        # Per-(job_id) dedup map for dispatch_skip emission. Keyed by job_id,
        # value is the most-recently-emitted reason string. Reset on broker
        # restart (best-effort observability — see Task 4 brief).
        "dispatch_skip_state": {},
    }
    app.state.shared = state
    app.state.SessionLocal = SessionLocal

    # Prometheus metrics: mounted as a sub-app so it bypasses the global bearer
    # token (mounts don't inherit router dependencies). Its /metrics path is
    # exempted from the tailnet ACL in auth.py so an in-cluster Prometheus can
    # scrape it. Exposes only aggregate job/worker counts.
    app.mount("/metrics", build_metrics_app(SessionLocal))

    # Server-side long-poll wake. /next-job (async) with wait_s>0 suspends on an
    # asyncio.Event until a mutation that could newly satisfy a match (submit /
    # terminal transition / requeue) or a bounded recheck elapses — WITHOUT
    # holding an anyio threadpool thread, so a fleet of long-pollers can't starve
    # /heartbeat and the other sync endpoints (M4). We wake liberally; spurious
    # wakes are harmless (the waiter just re-runs the pick and re-waits), and
    # _LONGPOLL_RECHECK_S backstops any wake site we miss.
    #
    # Wakes originate on sync threadpool threads (submit/complete/cancel/…) and
    # the sweep thread, so they're marshalled onto the loop via
    # call_soon_threadsafe. The event is SWAPPED (not just set) on each wake: a
    # waiter captures the current event before its claim attempt, so a wake that
    # fires during the attempt lands on the captured (now-set) event and the
    # waiter returns immediately instead of missing it.
    def _wake_dispatchers() -> None:
        loop = _loop_holder[0]
        if loop is None:
            return  # no async waiter has run yet — nothing is parked

        def _fire() -> None:
            ev = _wake_holder[0]
            _wake_holder[0] = asyncio.Event()
            ev.set()

        with suppress(RuntimeError):  # loop closed during shutdown — nothing to wake
            loop.call_soon_threadsafe(_fire)

    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__}

    # --- Unauthenticated probes. ------------------------------------------------
    # Exempted by exact path in auth._UNAUTHENTICATED_PATHS. They exist because a
    # generic HTTP monitor cannot send a bearer token: Uptime Kuma watches twelve
    # homelab services and jobd — the broker the whole fleet depends on — was the only
    # one it did NOT, purely because every route was behind the token wall.
    #
    # Deliberately mute: alive-or-not, ready-or-not, nothing else. No version, no
    # counts, no job data. /health keeps the version and keeps its auth, because the
    # container healthcheck must PROVE it is talking to jobd (scripts/healthcheck.py —
    # a probe that could not tell which daemon answered is a bug we have shipped).
    @app.get("/livez")
    def livez():
        """Process is up. Deliberately does not touch the DB."""
        return {"status": "alive"}

    @app.get("/readyz")
    def readyz(response: Response):
        """The broker can actually SERVE — round-trips a query to SQLite.

        /livez green + /readyz red = "the process is fine, the database is wedged",
        which says fix the DB rather than restart the process. Restarting would have
        destroyed the evidence.
        """
        try:
            with SessionLocal() as session:
                session.execute(text("SELECT 1"))
        except Exception as exc:
            # 503, not 500: "not ready" is a retryable state, and it is what every
            # monitor and orchestrator expects from a readiness probe.
            response.status_code = 503
            return {"status": "not_ready", "reason": f"{type(exc).__name__}: {exc}"[:200]}
        return {"status": "ready"}

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
        # Thin adapter. The submission logic — validation, effective-config cascade,
        # array fan-out, persistence, the H-1 TOCTOU cascade sweep, event emission —
        # lives in jobd.broker.submit.submit_job, callable without HTTP.
        #
        # NOTE: the tests that guard H-1 and the depends_on TOCTOU monkeypatch
        # `_serialization_warning` and `_FAILED_SIDE_TERMINAL` in the module where
        # submit_job resolves them — jobd.broker.submit, NOT here. Patching them on
        # jobd.app would now be a silent no-op. See that module's docstring.
        return submit_job(
            req,
            session_factory=SessionLocal,
            state=state,
            logs_dir=logs_dir,
            wake_dispatchers=_wake_dispatchers,
        )

    @app.get("/jobs", response_model=list[JobInfo])
    def list_jobs(
        response: Response,
        state_filter: str | None = None,
        project: str | None = None,
        warnings_only: bool = False,
        array_id: int | None = None,
        limit: int | None = Query(None, ge=1, le=LIST_LIMIT_MAX),
        offset: int = Query(0, ge=0),
    ):
        """List jobs, newest first.

        Pagination (audit 2026-07-12): this used to return EVERY row ever — on a
        broker with retention off that is the whole history, and `jobd_list` was
        already working around it by fetching everything and truncating
        client-side. `limit` is opt-in (None = all) rather than defaulted, because
        `graph` and `--array` build over the *complete* set and a silent default
        cap would quietly corrupt them. The full filtered count is always returned
        in the `X-Total-Count` header so a caller can show "N of M".
        """
        with SessionLocal() as session:
            conds = []
            if state_filter:
                conds.append(Job.state == state_filter)
            if project:
                conds.append(Job.project == project)
            if warnings_only:
                conds.append(Job.warning.is_not(None))
            if array_id is not None:
                conds.append(Job.array_id == array_id)

            total = session.execute(
                select(func.count()).select_from(Job).where(*conds)
            ).scalar_one()

            stmt = select(Job).where(*conds)
            # array members are ordered by index for readable `--array` output
            stmt = stmt.order_by(Job.array_index if array_id is not None else Job.id.desc())
            if limit is not None:
                stmt = stmt.limit(limit)
            if offset:
                stmt = stmt.offset(offset)

            jobs = session.execute(stmt).scalars().all()
            eta_ctx = _build_eta_ctx(session)
            response.headers["X-Total-Count"] = str(total)
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
    async def append_log(
        job_id: int,
        request: Request,
        x_jobd_worker: str | None = Header(default=None),
    ):
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            # Drop log chunks from a stale worker (M2): a partition-reclaimed job
            # re-dispatched elsewhere must not have the original worker's output
            # interleaved into the new run's log file.
            _reject_stale_worker(job.worker, x_jobd_worker, job_id, "log append")
        # Bounded read: reject an oversized chunk from the Content-Length header
        # when one is declared, and cap the streamed read either way — the old
        # `await request.body()` buffered an arbitrarily large body into memory
        # BEFORE the size check ran (audit 2026-07-05).
        try:
            declared = int(request.headers.get("content-length", ""))
        except ValueError:
            declared = None
        if declared is not None and declared > MAX_LOG_CHUNK_BYTES:
            raise HTTPException(status_code=413, detail="log chunk too large")
        received = bytearray()
        async for chunk in request.stream():
            received.extend(chunk)
            if len(received) > MAX_LOG_CHUNK_BYTES:
                raise HTTPException(status_code=413, detail="log chunk too large")
        body = bytes(received)
        log_file = job_log_path(logs_dir, job_id)
        with log_file.open("ab") as f:
            f.write(body)
        return {"bytes": len(body)}

    @app.post("/jobs/{job_id}/started", response_model=JobInfo)
    def started_job(job_id: int, x_jobd_worker: str | None = Header(default=None)):
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
            _reject_stale_worker(job.worker, x_jobd_worker, job_id, "started")
            # Guarded assigned->running: win only if the row is still ASSIGNED,
            # so a /started that races a concurrent sweeper orphan (or a cancel
            # that already went terminal) can't re-open a job that has left
            # ASSIGNED. Terminal / already-running states remain no-ops.
            won = _cas_state(session, job_id, (JobState.ASSIGNED,), state=JobState.RUNNING)
            if won:
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
            else:
                session.refresh(job)
            return _to_info(job)

    @app.post("/jobs/{job_id}/complete", response_model=JobInfo)
    def complete_job(
        job_id: int, payload: CompletePayload, x_jobd_worker: str | None = Header(default=None)
    ):
        exit_code = payload.exit_code
        final_state = payload.final_state
        termination_reason = payload.termination_reason
        if final_state is None:
            # Worker didn't specify — derive from exit code. This keeps the
            # depends_on cascade honest: nonzero rc must land in a failed-side
            # terminal so children that didn't opt into any_exit get cancelled.
            final_state = "completed" if exit_code in (0, None) else "failed"
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            # Refuse a terminal report from a stale worker (M2): a
            # partition-reclaimed job re-dispatched to a fresh worker must not be
            # terminal-ized (or have its outcome overwritten) by the original
            # worker that kept running. Checked before the CAS so the stale
            # /complete never wins the transition.
            _reject_stale_worker(job.worker, x_jobd_worker, job_id, "completion")
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
            # job_completed. The guard is a compare-and-swap (win only from a
            # non-terminal state) rather than a read-then-check so it can't race
            # a concurrent sweeper orphan: read-check-write let a late /complete
            # clobber a just-set ORPHANED back to COMPLETED (and vice versa).
            complete_values: dict = dict(
                state=final_state,
                exit_code=exit_code,
                finished_at=datetime.now(UTC),
                # Clear any pending signal — it's been honored (or irrelevant now
                # that the job is terminal). Otherwise a future reclaim/retry
                # would see a stale cancel signal and die on startup.
                signal=None,
            )
            if termination_reason is not None:
                complete_values["termination_reason"] = termination_reason
            won = _cas_state(session, job_id, NON_TERMINAL_STATES, **complete_values)
            if not won:
                session.refresh(job)
                return _to_info(job)
            session.refresh(job)
            cascaded = _cascade_on_parent_terminal(session, job)
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
            cancel_event_kw: dict | None = None
            cascaded: list[tuple[int, str]] = []
            # Guarded queued->cancelled. Only wins if the row is STILL queued:
            # a worker's atomic queued->assigned claim can land between our read
            # and this write, and without the WHERE state=queued guard we'd
            # clobber that claim to CANCELLED without setting `signal` — the
            # worker would then run the job to completion while the user is told
            # it was cancelled (the H3 cancel-vs-claim TOCTOU).
            if job.state == JobState.QUEUED:
                won = _cas_state(
                    session,
                    job_id,
                    (JobState.QUEUED,),
                    state=JobState.CANCELLED,
                    finished_at=datetime.now(UTC),
                )
                if won:
                    session.refresh(job)  # sync ORM state for the cascade below
                    cascaded = _cascade_on_parent_terminal(session, job)
                    cancel_event_kw = dict(prior_state=JobState.QUEUED.value, by="user")
                else:
                    # Lost the race to the claim — re-read and fall through to
                    # the running/assigned signal path below.
                    session.refresh(job)
            if cancel_event_kw is None and job.state in (JobState.RUNNING, JobState.ASSIGNED):
                # Signal-based cancel: the worker honors it on its next poll.
                # Guarded so a job that reached a terminal state between our read
                # and here doesn't get a stale 'cancel' signal stamped on it.
                prior_state = job.state.value if hasattr(job.state, "value") else job.state
                won = _cas_state(
                    session,
                    job_id,
                    (JobState.RUNNING, JobState.ASSIGNED),
                    signal="cancel",
                )
                if won:
                    cancel_event_kw = dict(prior_state=prior_state, by="user_signal_pending")
            # else: terminal-state cancel is a no-op; don't emit
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
            # Guarded so a job that reached a terminal state between the check
            # above and this write doesn't get a stale 'preempt' signal stamped
            # on it (which a later reclaim/retry would honor and mis-kill).
            _cas_state(session, job_id, (JobState.RUNNING, JobState.ASSIGNED), signal="preempt")
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

            # CAS on the states the candidate was selected in (same guard as
            # the sweeper's auto-preempt): a /complete or requeue landing
            # between the SELECT and this write must not get a stale 'preempt'
            # stamped over it (audit 2026-07-05 A1).
            won = _cas_state(
                session,
                candidate.id,
                (JobState.ASSIGNED, JobState.RUNNING),
                signal="preempt",
                warning=f"{_AUTO_PREEMPT_WARNING_PREFIX}{job.id} (manual)",
                warning_at=now,
            )
            session.commit()
            if not won:
                return {
                    "signaled": None,
                    "reason": "candidate left running/assigned before it could be signalled; retry",
                }
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
    def refuse_admission(
        job_id: int, payload: AdmissionRefusal, x_jobd_worker: str | None = Header(default=None)
    ):
        # Thin adapter. The admission decision tree — stale-worker rejection, the
        # ASSIGNED guard, pending-cancel honouring, and the requeue / terminal-fail /
        # cwd-exclusion / auto-preempt branches, each with its own CAS + refresh +
        # cascade + event teardown — lives in jobd.broker.admission.refuse_admission.
        return refuse_admission_service(
            job_id,
            payload,
            x_jobd_worker,
            session_factory=SessionLocal,
            logs_dir=logs_dir,
            wake_dispatchers=_wake_dispatchers,
        )

    @app.get("/jobs/{job_id}/output")
    def get_output(job_id: int, tail: int = 8192):
        """Return the last `tail` bytes of the job's captured stdout+stderr.

        The worker streams log chunks to /log, which the broker appends to
        logs_dir/<id>.log. This endpoint returns a tail of that file so
        callers can diagnose a failed job without SSHing to the worker.
        Returns 404 if the job doesn't exist, empty string if the file
        hasn't been created yet (worker crashed before any output).

        `pruned` distinguishes "log retention deleted this" from "the worker
        never captured anything" (audit 2026-07-12) — both leave no file on
        disk, but reporting a pruned log as empty output would make a job that
        emitted megabytes look like it produced nothing.
        """
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            log_pruned_at = job.log_pruned_at
        log_file = job_log_path(logs_dir, job_id)
        if not log_file.exists():
            return {
                "tail": "",
                "size_bytes": 0,
                "returned_bytes": 0,
                "truncated": False,
                "pruned": log_pruned_at is not None,
                "pruned_at": log_pruned_at.isoformat() if log_pruned_at else None,
            }
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
            "pruned": False,
            "pruned_at": None,
        }

    @app.get("/wait/{job_id}")
    async def wait_job(job_id: int):
        # Build the log filename from an explicit int() of the path param: it's
        # already int-typed by FastAPI, but the coercion makes it impossible for
        # a traversal sequence to reach the filename and clears CodeQL's
        # py/path-injection sink on the reads below (a bare int-typed param is
        # still modeled as a tainted string by that query).
        log_file = job_log_path(logs_dir, job_id)

        async def event_generator():
            position = 0
            # Incremental decoder so a multibyte UTF-8 char split across two
            # bounded reads isn't corrupted into replacement chars.
            decoder = codecs.getincrementaldecoder("utf-8")("replace")

            def drain_new_bytes():
                # Yield log events for all bytes past `position`, reading in
                # bounded slices so a large backlog is never pulled into memory
                # in one allocation. The file is reopened per slice so no fd is
                # held open across an SSE yield/await.
                nonlocal position
                while True:
                    if not log_file.exists():
                        return
                    with log_file.open("rb") as f:
                        f.seek(position)
                        chunk = f.read(WAIT_STREAM_CHUNK_BYTES)
                    if not chunk:
                        return
                    position += len(chunk)
                    text = decoder.decode(chunk)
                    if text:
                        yield {"event": "log", "data": text}

            while True:
                # Read new log bytes since last position
                for ev in drain_new_bytes():
                    yield ev

                # Check job state
                with SessionLocal() as session:
                    job = session.get(Job, job_id)

                if job is None:
                    yield {"event": "error", "data": "no such job"}
                    return

                if JobState(job.state) in TERMINAL_STATES:
                    # Final drain: the worker may have written its last bytes
                    # between our read above and flipping to a terminal state.
                    for ev in drain_new_bytes():
                        yield ev
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        yield {"event": "log", "data": tail}
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
            # Assigned unconditionally, including None: a worker that stops
            # reporting a version IS an older worker, and pinning the last-known
            # value would make the fleet look newer than it is.
            worker.version = hb.version
            worker.state = "online"
            if hb.in_flight_pids is not None:
                worker.in_flight_pids_json = json.dumps(hb.in_flight_pids)
            orphan_records: list[tuple[int, str]] = []
            cascade_records: list[tuple[int, str, list[tuple[int, str]]]] = []
            requeued: list[int] = []
            cancelled_records: list[tuple[int, str, str]] = []
            resurrected: list[tuple[int, str]] = []
            restored: list[tuple[int, str]] = []
            if hb.in_flight_job_ids is not None:
                (
                    orphan_records,
                    cascade_records,
                    requeued,
                    cancelled_records,
                    resurrected,
                    restored,
                ) = _reconcile_worker_in_flight(session, hb.host, set(hb.in_flight_job_ids))
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
                version=hb.version,
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
        for jid, proj, prior_state in cancelled_records:
            _emit_event(
                logs_dir,
                "job_cancelled",
                source="broker",
                job_id=jid,
                project=proj,
                prior_state=prior_state,
                by="user",
                via="reconcile",
            )
        for jid, proj in resurrected:
            _emit_event(
                logs_dir,
                "job_resurrected",
                source="broker",
                job_id=jid,
                project=proj,
                worker=hb.host,
                prior_state="orphaned",
                note="worker reported it still in flight; the worker-is-gone premise was false",
            )
        for jid, proj in restored:
            _emit_event(
                logs_dir,
                "job_uncancelled",
                source="broker",
                job_id=jid,
                project=proj,
                by="resurrect",
                note="parent was resurrected; this child had been cascade-cancelled",
            )
        # Requeues make jobs dispatchable again; orphans and cancels are
        # failed-side terminals that can unblock any-exit dependents (and their
        # cascades). All three change what the matcher would pick — wake
        # long-pollers rather than waiting out the recheck backstop. So do
        # `restored` children, which go back to QUEUED and are dispatchable now.
        if requeued or orphan_records or cancelled_records or restored:
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
                    version=w.version,
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
    async def next_job(q: NextJobQuery):
        from jobd.matcher import pick_next_job

        # Capture the running loop so sync-thread wake sites can reach it (the
        # lifespan sets it too; this covers callers that skip lifespan, e.g.
        # TestClient without a context manager).
        _loop_holder[0] = asyncio.get_running_loop()

        def _attempt() -> JobInfo | None:
            """One claim attempt against the live queue. Returns the claimed job
            or None (no match OR lost race — both mean 'nothing for you now')."""
            with SessionLocal() as session:
                # Order in SQL rather than sorting in Python. `pick_next_job` applies
                # the same (priority desc, submitted_at asc) order itself, so this is
                # not a behavior change — it just lets the index do the work, and it
                # makes the scan order deterministic for the fits-on-worker walk.
                all_queued = (
                    session.execute(
                        select(Job)
                        .where(Job.state == JobState.QUEUED)
                        .order_by(Job.priority.desc(), Job.submitted_at.asc())
                    )
                    .scalars()
                    .all()
                )
                # One SELECT for every parent of every queued job, instead of a
                # point-read per parent per job (N+1) on every attempt by every
                # parked worker. See _deps_satisfied_bulk on why this is *fresher*
                # than the loop it replaces, not just cheaper.
                satisfied_ids = _deps_satisfied_bulk(all_queued, session)
                queued: list[Job] = [j for j in all_queued if j.id in satisfied_ids]
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
                # Drop jobs this host already refused for a missing cwd (B): the
                # per-job exclusion set guarantees a refused job re-routes to a
                # host that has the path and never hot-loops back here.
                queued = [j for j in queued if q.host not in _excluded_workers(j)]
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
                    skip_state: dict[tuple[int, str], str] = state["dispatch_skip_state"]
                    queued_by_id = {j.id: j for j in queued}
                    for job_id, reason in skips:
                        # Key on (job, worker), not job alone. The reason is
                        # computed against THIS worker, so the same job yields
                        # different reasons on different hosts — keyed by job_id
                        # only, each worker's answer overwrote the last, the
                        # "changed?" guard was true on every poll, and the event
                        # fired every time (see BrokerState.dispatch_skip_state).
                        key = (job_id, q.host)
                        if skip_state.get(key) != reason:
                            skip_state[key] = reason
                            _emit_event(
                                logs_dir,
                                "dispatch_skip",
                                source="broker",
                                job_id=job_id,
                                project=queued_by_id[job_id].project,
                                worker=q.host,
                                reason=reason,
                            )
                    # Drop dedup entries for jobs this worker can no longer see,
                    # so a job that ran or was cancelled doesn't leak memory and a
                    # resubmit gets a fresh slot. Scoped to THIS worker's keys:
                    # `queued` has already been filtered by mount_roots and this
                    # host's exclusion set, so a job that is merely invisible *here*
                    # is still queued elsewhere, and evicting other hosts' entries
                    # for it would make them re-emit on their next poll.
                    # pop, not del: concurrent /next-job attempts share this dict
                    # and another thread can remove the same key underneath us.
                    for stale in [
                        k for k in skip_state if k[1] == q.host and k[0] not in queued_by_id
                    ]:
                        skip_state.pop(stale, None)
                    return None
                # Atomic claim: only one worker can transition queued -> assigned
                result = session.execute(
                    Job.__table__.update()  # type: ignore[attr-defined]  # SQLAlchemy: Table.update on __table__
                    .where(Job.id == pick.id, Job.state == JobState.QUEUED)
                    .values(
                        state=JobState.ASSIGNED,
                        worker=q.host,
                        started_at=datetime.now(UTC),
                        # A signal on a QUEUED row is always a leftover from a
                        # previous incarnation (cancel of a queued job goes
                        # straight to CANCELLED; preempt applies only to
                        # assigned/running). Clear it at claim so a fresh run
                        # can never inherit a stale cancel/preempt an unguarded
                        # writer stamped mid-requeue (audit 2026-07-05 A1).
                        signal=None,
                    )
                )
                session.commit()
                if result.rowcount == 0:  # type: ignore[attr-defined]  # SQLAlchemy: CursorResult.rowcount
                    return None  # lost race; re-attempt / re-wait
                session.refresh(pick)
                # SQLite stores naive UTC; mirror sweep_once's naive-to-naive
                # comparison so the subtract can't mix aware and naive.
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
                # The claiming worker needs the REAL env to run the job — this is
                # the one read surface that must not redact (all other _to_info
                # callers keep the default redact_env=True). See _redact_env.
                return _to_info(pick, redact_env=False)

        # Long-poll: hold the request up to q.wait_s, re-attempting whenever a
        # dispatcher wake fires (or every _LONGPOLL_RECHECK_S as a backstop for
        # any wake site we miss). wait_s=0 → single attempt, legacy behavior.
        # The blocking claim runs in a thread (asyncio's default executor, NOT
        # the anyio pool that serves the sync endpoints) and the wait suspends on
        # the loop, so a parked long-poll holds no threadpool token (M4).
        wait_s = max(0.0, float(q.wait_s or 0.0))
        deadline = time.monotonic() + wait_s
        while True:
            # Capture the wake event BEFORE the attempt so a wake that fires
            # during the attempt isn't lost (it sets this exact event).
            wake = _wake_holder[0]
            info = await asyncio.to_thread(_attempt)
            if info is not None:
                return info
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return None
            # recheck backstop elapsed with no wake — loop and re-attempt
            with suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=min(remaining, _LONGPOLL_RECHECK_S))

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
    def set_project_priority(name: str, payload: SetPriorityRequest):
        priority = max(0, min(100, payload.priority))
        existing = state["projects"].get(name)
        if existing is None:
            state["projects"][name] = ProjectEntry(priority=priority)
        else:
            existing.priority = priority
        _persist_projects(state)
        return _projects_to_jsonable(state["projects"])

    @app.post("/projects/{name}/nudge")
    def nudge_project_priority(name: str, payload: NudgePriorityRequest):
        delta = payload.delta
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
        # Re-read the git baseline AND re-apply the runtime overrides overlay, so
        # a `git pull` of projects.yaml takes effect without discarding priorities
        # set at runtime via `job projects set/nudge` (audit 2026-07-12).
        projects, base_priorities = load_effective_projects(
            state["paths"]["projects"], state["paths"]["project_overrides"]
        )
        state["projects"] = projects
        state["base_priorities"] = base_priorities
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

        eff = resolve_effective_config(req, state["projects"], profile_spec)
        # requires is carried as the JobRequires OBJECT internally (so /submit
        # can serialize it); the API surfaces it model_dumped. Every other field
        # is the shared FieldResolution verbatim — same precedence /submit runs.
        requires_value = eff.requires.value
        return ResolvedConfig(
            project=req.project,
            effective_priority=eff.priority,
            effective_host_pin=eff.host_pin,
            effective_max_wall_s=eff.max_wall_s,
            effective_idle_timeout_s=eff.idle_timeout_s,
            effective_checkpoint_grace_s=eff.checkpoint_grace_s,
            effective_preemptible=eff.preemptible,
            effective_requires=FieldResolution(
                value=requires_value.model_dump() if requires_value is not None else None,
                source=eff.requires.source,
            ),
            effective_escalate_to_arc=eff.escalate_to_arc,
            submit_warning=eff.unknown_project_warning,
        )

    def _prune_old_jobs(now: datetime | None = None) -> int:
        return prune_old_jobs(SessionLocal, logs_dir, now)

    def _sweep_once() -> None:
        sweep_once(SessionLocal, logs_dir, _wake_dispatchers)

    # Test seams, scoped to THIS app instance. These used to be injected into
    # module globals ("last build_app() wins"), which silently bound
    # jobd.app._sweep_once to another app's engine whenever two apps existed
    # in one process, kept that engine alive via the closure, and hid the
    # names from static analysis (audit 2026-07-05, A5).
    app.state.sweep_once = _sweep_once
    app.state.prune_old_jobs = _prune_old_jobs
    app.state.engine = engine

    return app
