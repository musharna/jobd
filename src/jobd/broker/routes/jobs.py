"""Job lifecycle: /submit, /jobs..., /wait/{job_id}.

Stage-3 split (backlog 2026-07-15): endpoint bodies are VERBATIM from
app.py's build_app — build_router unpacks BrokerDeps into the same local
names the closures always captured, so the move is byte-identical at the
body level and the whole suite passes unchanged.
"""

from __future__ import annotations

import asyncio
import codecs
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from sqlalchemy import func, select
from sse_starlette.sse import EventSourceResponse

from jobd.broker.admission import refuse_admission as refuse_admission_service
from jobd.broker.constants import (
    _AUTO_PREEMPT_WARNING_PREFIX,
    LIST_LIMIT_MAX,
    MAX_LOG_CHUNK_BYTES,
    NON_TERMINAL_STATES,
    WAIT_STREAM_CHUNK_BYTES,
)
from jobd.broker.context import BrokerDeps
from jobd.broker.events import _emit_event
from jobd.broker.jobinfo import _build_eta_ctx, _to_info
from jobd.broker.joblog import job_log_path
from jobd.broker.scheduling import _build_snapshots
from jobd.broker.state import (
    _cas_state,
    _cascade_on_parent_terminal,
    _emit_cascade_cancellations,
    _reject_stale_worker,
)
from jobd.broker.submit import submit_job
from jobd.db import Job, Worker
from jobd.matcher import eligible_workers
from jobd.models import (
    TERMINAL_STATES,
    AdmissionRefusal,
    CompletePayload,
    JobInfo,
    JobRequires,
    JobState,
    JobSubmit,
)


def build_router(deps: BrokerDeps) -> APIRouter:
    router = APIRouter()
    SessionLocal = deps.session_local
    logs_dir = deps.logs_dir
    state = deps.state
    _wake_dispatchers = deps.wake_dispatchers

    # response_model=None: dry-run returns a plan dict, live returns JobInfo.
    # Both paths are JSON-serializable; FastAPI dispatches per-response.
    @router.post("/submit", response_model=None)
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

    @router.get("/jobs", response_model=list[JobInfo])
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

    @router.get("/jobs/{job_id}", response_model=JobInfo)
    def get_job(job_id: int):
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            eta_ctx = _build_eta_ctx(session)
            return _to_info(job, eta_ctx)

    @router.post("/jobs/{job_id}/log")
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

    @router.post("/jobs/{job_id}/started", response_model=JobInfo)
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

    @router.post("/jobs/{job_id}/complete", response_model=JobInfo)
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

    @router.post("/jobs/{job_id}/cancel", response_model=JobInfo)
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

    @router.post("/jobs/{job_id}/preempt", response_model=JobInfo)
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

    @router.post("/jobs/{job_id}/preempt-blockers")
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

    @router.get("/jobs/{job_id}/signal")
    def get_signal(job_id: int):
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            return {"signal": job.signal}

    @router.post("/jobs/{job_id}/checkpoint-complete")
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

    @router.post("/jobs/{job_id}/refuse-admission", response_model=JobInfo)
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

    @router.get("/jobs/{job_id}/output")
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

    @router.get("/wait/{job_id}")
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

                # Check job state — off the event loop: this generator runs ON
                # the loop (async SSE), and a sync session.get against a
                # contended SQLite would otherwise stall every request in the
                # process for its duration, once per waiting client per tick.
                def _get_job():
                    with SessionLocal() as session:
                        return session.get(Job, job_id)

                job = await asyncio.to_thread(_get_job)

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

    return router
