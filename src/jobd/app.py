"""FastAPI application factory for jobd.

Wiring only: engine/session setup, config load, auth install, the sweep
loop, the /next-job long-poll wake plumbing, and test seams. The endpoint
closures live in ``jobd.broker.routes.*`` (Stage 3 of the 2026-07-02 split,
landed 2026-07-15): each module's ``build_router(deps)`` unpacks BrokerDeps
into the same local names the closures always captured, so their bodies moved
out of here verbatim. Test seams that used to be patched on ``jobd.app``
(``_cas_state``, ``WAIT_STREAM_CHUNK_BYTES``) are patched on the routes module
that resolves them now — same precedent as the submit-service split.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from jobd import __version__
from jobd.auth import install_tailnet_acl, require_token
from jobd.broker.constants import (
    AUTO_PREEMPT_MIN_RUNTIME_SECONDS,  # noqa: F401  # re-exported: tests read jobd.app.AUTO_PREEMPT_MIN_RUNTIME_SECONDS
    SWEEP_INTERVAL_SECONDS,
)
from jobd.broker.context import BrokerDeps, BrokerState
from jobd.broker.routes.config import build_router as build_config_router
from jobd.broker.routes.events import build_router as build_events_router
from jobd.broker.routes.jobs import build_router as build_jobs_router
from jobd.broker.routes.probes import build_router as build_probes_router
from jobd.broker.routes.workers import build_router as build_workers_router
from jobd.broker.sweeper import (
    prune_old_jobs,
    scrub_terminal_env,
    sweep_once,
    warn_version_drift,
)
from jobd.config import (
    load_classifier_rules,
    load_effective_projects,
    load_profiles,
)
from jobd.db import init_db, migrate
from jobd.metrics import build_metrics_app

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

    deps = BrokerDeps(
        session_local=SessionLocal,
        logs_dir=logs_dir,
        state=state,
        wake_dispatchers=_wake_dispatchers,
        loop_holder=_loop_holder,
        wake_holder=_wake_holder,
    )
    # Original registration order preserved: probes, workers' /gpu-holders and
    # the jobs surface, then heartbeat/dispatch, events, and the config surface.
    app.include_router(build_probes_router(deps))
    app.include_router(build_jobs_router(deps))
    app.include_router(build_workers_router(deps))
    app.include_router(build_events_router(deps))
    app.include_router(build_config_router(deps))

    def _prune_old_jobs(now: datetime | None = None) -> int:
        return prune_old_jobs(SessionLocal, logs_dir, now)

    def _scrub_terminal_env(now: datetime | None = None) -> int:
        return scrub_terminal_env(SessionLocal, logs_dir, now)

    def _warn_version_drift(now: datetime | None = None) -> int:
        return warn_version_drift(SessionLocal, logs_dir, now)

    def _sweep_once() -> None:
        sweep_once(SessionLocal, logs_dir, _wake_dispatchers)

    # Test seams, scoped to THIS app instance. These used to be injected into
    # module globals ("last build_app() wins"), which silently bound
    # jobd.app._sweep_once to another app's engine whenever two apps existed
    # in one process, kept that engine alive via the closure, and hid the
    # names from static analysis (audit 2026-07-05, A5).
    app.state.sweep_once = _sweep_once
    app.state.prune_old_jobs = _prune_old_jobs
    app.state.scrub_terminal_env = _scrub_terminal_env
    app.state.warn_version_drift = _warn_version_drift
    app.state.engine = engine

    return app
