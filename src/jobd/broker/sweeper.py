"""Background sweep: scheduling-timeout, worker offline/stale transitions,
orphan reclaim, auto-preempt/blocker warnings, and retention pruning.

Module-level functions taking their broker deps (session factory, logs dir, and
the long-poll wake callback) explicitly, so the app factory keeps only a thin
wrapper and the whole sweep pass is testable in isolation.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from jobd.broker.constants import (
    _AUTO_PREEMPT_WARNING_PREFIX,
    _BLOCKED_WARNING_PREFIX,
    _UNMATCHEABLE_WARNING_PREFIX,
    DEAD_WORKER_SECONDS,
    IDEMPOTENT_RECLAIM_SECONDS,
    JOB_RETENTION_DAYS_DEFAULT,
    LOG_RETENTION_DAYS_DEFAULT,
    OFFLINE_AFTER_SECONDS,
    QUEUE_BLOCKED_THRESHOLD_SECONDS,
    STALE_WORKER_THRESHOLD_S_DEFAULT,
    UNMATCHEABLE_THRESHOLD_SECONDS,
    WALL_CLOCK_BACKSTOP_GRACE_SECONDS,
)
from jobd.broker.events import _emit_event
from jobd.broker.joblog import job_log_path
from jobd.broker.scheduling import (
    _build_snapshots,
    _find_nonpreemptible_blocker,
    _find_preemptible_candidate,
)
from jobd.broker.state import (
    _cas_state,
    _cascade_on_parent_terminal,
    _emit_cascade_cancellations,
    _requeue_or_honor_cancel,
)
from jobd.db import Job, Worker
from jobd.matcher import eligible_workers, selectors_only_match
from jobd.models import TERMINAL_STATES, JobRequires, JobState

log = logging.getLogger("jobd")


def prune_old_jobs(session_local, logs_dir: Path, now: datetime | None = None) -> int:
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
    with session_local() as session:
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
            job_log_path(logs_dir, jid).unlink()
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


def prune_old_logs(session_local, logs_dir: Path, now: datetime | None = None) -> int:
    """Unlink the per-job ``.log`` of terminal jobs finished longer ago than
    ``JOBD_LOG_RETENTION_DAYS``, **keeping the job row**. Returns the number of
    files actually unlinked; 0 days disables it.

    Logs and rows are pruned on separate clocks because their cost/value ratio
    differs by ~400x — see ``LOG_RETENTION_DAYS_DEFAULT`` for the measurements
    and the reasoning. In short: the log dir is the real disk cost, while the
    rows are cheap and feed the ETA estimator, so bounding one should not
    require discarding the other.

    ``log_pruned_at`` is stamped on every job whose log is dealt with (unlinked,
    or already absent). That keeps this scan shrinking monotonically instead of
    re-stat'ing all of history on every 30s sweep, and it lets ``job logs``
    distinguish "pruned by retention" from "the worker never captured output".
    A job whose unlink genuinely FAILED is deliberately left unstamped so the
    next sweep retries it.
    """
    raw = os.environ.get("JOBD_LOG_RETENTION_DAYS", "").strip()
    try:
        retention_days = int(raw) if raw else LOG_RETENTION_DAYS_DEFAULT
    except ValueError:
        retention_days = LOG_RETENTION_DAYS_DEFAULT
    if retention_days <= 0:
        return 0
    if now is None:
        now = datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(days=retention_days)
    terminal = [s.value for s in TERMINAL_STATES]
    with session_local() as session:
        candidates = list(
            session.execute(
                select(Job.id).where(
                    Job.state.in_(terminal),
                    Job.finished_at.is_not(None),
                    Job.finished_at < cutoff,
                    Job.log_pruned_at.is_(None),
                )
            ).scalars()
        )
        if not candidates:
            return 0
        settled: list[int] = []
        unlinked = 0
        for jid in candidates:
            try:
                job_log_path(logs_dir, jid).unlink()
                unlinked += 1
                settled.append(jid)
            except FileNotFoundError:
                # Nothing to remove (job never produced output, or a previous
                # prune raced). Still settle it so we stop re-checking.
                settled.append(jid)
            except OSError as e:
                # Leave UNSTAMPED so the next sweep retries this one.
                log.warning("log retention: could not unlink %s.log: %s", jid, e)
        if settled:
            session.execute(
                Job.__table__.update()  # type: ignore[attr-defined]  # SQLAlchemy: Table.update on __table__
                .where(Job.id.in_(settled))
                .values(log_pruned_at=now)
            )
            session.commit()
    if unlinked:
        _emit_event(
            logs_dir,
            "logs_pruned",
            source="broker",
            count=unlinked,
            retention_days=retention_days,
        )
    return unlinked


def sweep_once(session_local, logs_dir: Path, wake_dispatchers: Callable[[], None]) -> None:
    """One full sweep pass. In order: expire queued jobs past their
    scheduling_timeout_s (with dependency cascade); mark silent workers
    offline/stale; reclaim ASSIGNED jobs from dead workers; reclaim or orphan
    RUNNING jobs (wall-clock backstop + dead-worker check, honoring pending
    cancels); then the phase-2 queue probes — auto-preempt a blocker for a
    starved high-priority job, set/clear unmatcheable + blocked warnings — and
    finally retention pruning. Events are emitted only after each phase's
    commit."""
    # SQLite stores datetimes as naive UTC; compare naive-to-naive.
    now = datetime.now(UTC).replace(tzinfo=None)
    going_offline_records: list[tuple[str, datetime | None]] = []
    scheduling_timeout_records: list[tuple[int, str, int]] = []
    # Declared up here (not at the running-reclaim loop) so the
    # scheduling_timeout cascade above can append to them too; all emitted
    # once after the single commit below.
    orphan_records: list[tuple[int, str, str | None, datetime | None, str]] = []
    cascade_records: list[tuple[int, str, list[tuple[int, str]]]] = []
    # Requeues (RUNNING/ASSIGNED -> QUEUED) are the one state change that makes a job
    # newly dispatchable, and they were the only outcome the sweep did NOT record.
    # Tracked so the dispatcher wake below can fire only on a sweep that changed
    # something, instead of broadcasting on every pass.
    requeued_ids: list[int] = []
    # (job_id, project, prior_state) — reclaim found a pending user cancel and
    # honored it instead of requeuing (A2).
    cancelled_records: list[tuple[int, str, str]] = []
    with session_local() as session:
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
            # M-1 (audit 2026-07-10): key the scheduling_timeout on the last
            # time the job entered QUEUED, not its original submit — a job that
            # dispatched, ran, then got reclaimed on a worker death must get a
            # fresh queue clock (last_enqueued_at is reset on every requeue).
            # NULL on pre-migration rows → fall back to submitted_at.
            queue_clock = j.last_enqueued_at or j.submitted_at
            deadline_age_s = (now - queue_clock).total_seconds()
            if deadline_age_s <= j.scheduling_timeout_s:
                continue
            # Guarded so a job the matcher claims (queued->assigned) at the
            # same instant isn't clobbered back to a terminal
            # scheduling_timeout: the sweeper reads QUEUED then writes,
            # racing the atomic claim. If the claim won, skip — the job is
            # dispatching, not stuck. (The orphan/reclaim writes below are
            # instead gated by their worker-liveness check, which a live
            # /complete's fresh heartbeat can't slip past.)
            won = _cas_state(
                session,
                j.id,
                (JobState.QUEUED,),
                state=JobState.SCHEDULING_TIMEOUT.value,
                finished_at=now,
                termination_reason="scheduling_timeout",
                signal=None,
            )
            if not won:
                continue
            # Cascade over the timed-out parent: it never dispatched, so it
            # can never produce the output a default-policy dependent needs.
            # Without this the child strands in QUEUED forever — the matcher
            # won't dispatch it (deps unsatisfied) and nothing else cancels
            # it. any-exit dependents are unblocked separately by
            # _deps_satisfied. Reverses the 2026-05-18 S3 "out of scope"
            # call, which left non-any-exit children stranded (audit
            # 2026-07-01 H2; the deps test only passed by calling the hook
            # manually).
            session.refresh(j)  # sync ORM state so the cascade sees the terminal
            cascaded = _cascade_on_parent_terminal(session, j)
            if cascaded:
                cascade_records.append((j.id, j.state, cascaded))
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
            # Guarded write (WHERE re-checks the cutoff): a heartbeat committing
            # between the SELECT above and this write must not be clobbered to
            # offline — the worker just proved it's alive. rowcount 0 = it won.
            fresh = session.execute(
                Worker.__table__.update()  # type: ignore[attr-defined]  # SQLAlchemy: Table.update on __table__
                .where(
                    Worker.host == w.host,
                    Worker.last_heartbeat < offline_cutoff,
                    Worker.state != "offline",
                )
                .values(state="offline")
            )
            if not fresh.rowcount:
                continue
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
            # Same guarded write as the offline transition above: don't mark a
            # worker stale over a heartbeat that landed after our SELECT.
            fresh = session.execute(
                Worker.__table__.update()  # type: ignore[attr-defined]  # SQLAlchemy: Table.update on __table__
                .where(
                    Worker.host == w.host,
                    Worker.last_heartbeat < stale_cutoff,
                    Worker.state == "online",
                )
                .values(state="stale")
            )
            if not fresh.rowcount:
                continue
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
            w = session.execute(select(Worker).where(Worker.host == j.worker)).scalar_one_or_none()
            if w is None or w.last_heartbeat < cutoff:
                # CAS-guarded (WHERE state=ASSIGNED): the reclaim DECISION is
                # gated by worker-liveness, but the WRITE must still not clobber
                # a /complete that a briefly-revived worker commits between this
                # pass's SELECT and its trailing commit (review finding F1). If
                # the row already left ASSIGNED, skip it. Clears a pending
                # preempt signal (M1) on the requeue; a pending user CANCEL is
                # honored — the job goes CANCELLED, not back to QUEUED (A2).
                outcome = _requeue_or_honor_cancel(
                    session,
                    j.id,
                    (JobState.ASSIGNED,),
                    now=now,
                    state=JobState.QUEUED,
                    worker=None,
                    started_at=None,
                )
                if outcome == "cancelled":
                    session.refresh(j)  # sync ORM state so the cascade fires
                    cascaded = _cascade_on_parent_terminal(session, j)
                    if cascaded:
                        cascade_records.append((j.id, j.state, cascaded))
                    cancelled_records.append((j.id, j.project, JobState.ASSIGNED.value))
                elif outcome == "requeued":
                    requeued_ids.append(j.id)

        # Pre-launch CRIT-2: reclaim RUNNING jobs whose worker has died.
        # A worker crash mid-run otherwise strands the job in RUNNING
        # forever — no /complete ever lands, dependents never unblock, and
        # /wait never returns. Idempotent jobs are safe to re-run, so we
        # requeue them like the ASSIGNED path above; non-idempotent jobs
        # already had side effects, so we transition them to ORPHANED (a
        # failed-side terminal state that cascades to dependents) rather
        # than silently re-running a job that may not be re-runnable.
        # (orphan_records / cascade_records declared at the top of the sweep
        # so the scheduling_timeout cascade shares them.)
        running = session.execute(select(Job).where(Job.state == JobState.RUNNING)).scalars().all()
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
                    # CAS-guarded (WHERE state=RUNNING) so a concurrent
                    # /complete isn't clobbered back to ORPHANED (F1).
                    if not _cas_state(
                        session,
                        j.id,
                        (JobState.RUNNING,),
                        state=JobState.ORPHANED.value,
                        finished_at=now,
                        termination_reason="wall_clock_exceeded",
                        signal=None,
                    ):
                        continue
                    session.refresh(j)  # sync ORM state so the cascade sees ORPHANED
                    cascaded = _cascade_on_parent_terminal(session, j)
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
            w = session.execute(select(Worker).where(Worker.host == j.worker)).scalar_one_or_none()
            if w is not None and w.last_heartbeat >= cutoff:
                continue  # worker still alive — leave the job running
            last_hb = w.last_heartbeat if w is not None else None
            # Both branches CAS-guard (WHERE state=RUNNING) so a /complete from
            # a briefly-revived worker isn't clobbered (F1). M1: clear the stale
            # preempt signal on the requeue; a pending user CANCEL is honored —
            # CANCELLED, not a silent re-run (A2).
            if idempotent:
                outcome = _requeue_or_honor_cancel(
                    session,
                    j.id,
                    (JobState.RUNNING,),
                    now=now,
                    state=JobState.QUEUED,
                    worker=None,
                    started_at=None,
                )
                if outcome == "cancelled":
                    session.refresh(j)  # sync ORM state so the cascade fires
                    cascaded = _cascade_on_parent_terminal(session, j)
                    if cascaded:
                        cascade_records.append((j.id, j.state, cascaded))
                    cancelled_records.append((j.id, j.project, JobState.RUNNING.value))
                elif outcome == "requeued":
                    requeued_ids.append(j.id)
            else:
                if not _cas_state(
                    session,
                    j.id,
                    (JobState.RUNNING,),
                    state=JobState.ORPHANED.value,
                    finished_at=now,
                    termination_reason="worker_died",
                    signal=None,
                ):
                    continue
                session.refresh(j)  # sync ORM state so the cascade sees ORPHANED
                cascaded = _cascade_on_parent_terminal(session, j)
                cascade_records.append((j.id, j.state, cascaded))
                orphan_records.append((j.id, j.project, j.worker, last_hb, "worker_died"))

        session.commit()
        # Reclaims/requeues and orphan cascades above may have made jobs
        # dispatchable — wake any long-polling workers.
        #
        # Only when something ACTUALLY changed, though. This used to fire on every
        # pass, i.e. unconditionally every SWEEP_INTERVAL_SECONDS, which broadcast a
        # wake to every parked /next-job long-poll; each then ran a full claim attempt
        # (queue scan + dependency reads + fit walk) and all but at most one returned
        # None. The overwhelmingly common sweep is a no-op, and a sweep that changed
        # no state cannot have made anything newly dispatchable, so there is nothing
        # to wake for.
        #
        # Safe by construction: a missed wake is not a stall. Parked workers re-attempt
        # every _LONGPOLL_RECHECK_S (10s) regardless of wakes, precisely as a backstop
        # for any wake site that doesn't fire — so the worst case if this predicate is
        # ever too narrow is up to 10s of extra queue latency, never a stuck job.
        if requeued_ids or cancelled_records or orphan_records or cascade_records:
            wake_dispatchers()
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
        for jid, proj, prior_state in cancelled_records:
            _emit_event(
                logs_dir,
                "job_cancelled",
                source="broker",
                job_id=jid,
                project=proj,
                prior_state=prior_state,
                by="user",
                via="sweeper_reclaim",
            )
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
        workers = session.execute(select(Worker).where(Worker.state == "online")).scalars().all()
        snapshots = _build_snapshots(list(workers))
        host_list = [w.host for w in workers]
        # Phase-2 events are collected here and emitted after the commit below —
        # same emit-after-commit invariant as phase 1 (M3): a failed commit must
        # not leave events.jsonl claiming a signal/warning the DB never recorded.
        auto_preempt_records: list[dict] = []
        sweep_warning_records: list[tuple[int, str, str]] = []
        for j in stale_queued:
            sq_req: JobRequires | None = None
            if j.requires_json and j.requires_json != "{}":
                try:
                    sq_req = JobRequires.model_validate_json(j.requires_json)
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
                requires=sq_req,
            )
            # sq_req=None means job matches any worker by definition — an empty
            # fleet is a queueing delay, not a capability mismatch.
            matcheable = sq_req is None or any(selectors_only_match(shim, ws) for ws in snapshots)

            blocker_warning: str | None = None
            if matcheable:
                age_s = (now - j.submitted_at).total_seconds()
                if age_s >= QUEUE_BLOCKED_THRESHOLD_SECONDS:
                    elig = eligible_workers(sq_req, j.host_pin, snapshots)
                    if elig:
                        candidate = _find_preemptible_candidate(elig, j, session, now)
                        if candidate is not None:
                            # CAS on the states the candidate was selected in: a
                            # /complete or reconcile-requeue landing between the
                            # SELECT and this write must not get a stale
                            # 'preempt' stamped over it — a requeued job would
                            # carry the signal into its next claim and be killed
                            # seconds into the fresh run (audit 2026-07-05 A1).
                            won = _cas_state(
                                session,
                                candidate.id,
                                (JobState.ASSIGNED, JobState.RUNNING),
                                signal="preempt",
                                warning=f"{_AUTO_PREEMPT_WARNING_PREFIX}{j.id}",
                                warning_at=now,
                            )
                            if won:
                                auto_preempt_records.append(
                                    dict(
                                        job_id=candidate.id,
                                        project=candidate.project,
                                        cause="sweeper",
                                        queued_job=j.id,
                                        worker=candidate.worker,
                                        queued_priority=j.priority,
                                        candidate_priority=candidate.priority,
                                        queue_age_s=int(age_s),
                                    )
                                )
                        else:
                            blocker = _find_nonpreemptible_blocker(elig, session)
                            if blocker is not None:
                                # Deliberately carries no queue age. This string is the
                                # dedup key (`if j.warning != blocker_warning` below), so
                                # embedding a value that ticks on its own makes the guard
                                # always fire: the old "queue-age {N}m: ..." re-emitted an
                                # event and rewrote the row every minute a job stayed
                                # blocked. Identity of the blocker is what changed or
                                # didn't; the age is derivable from submitted_at.
                                blocker_warning = (
                                    f"{_BLOCKED_WARNING_PREFIX}non-preemptible job "
                                    f"{blocker.id} on {blocker.worker} is holding the only "
                                    f"eligible worker; preempt with `job preempt {blocker.id}`"
                                )

            if not matcheable:
                new_w = (
                    f"{_UNMATCHEABLE_WARNING_PREFIX} none of {host_list} "
                    f"advertise required capabilities"
                )
                if j.warning != new_w:
                    j.warning = new_w
                    j.warning_at = now
                    sweep_warning_records.append((j.id, j.project, new_w))
            elif blocker_warning is not None:
                if j.warning != blocker_warning:
                    j.warning = blocker_warning
                    j.warning_at = now
                    sweep_warning_records.append((j.id, j.project, blocker_warning))
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
        for rec in auto_preempt_records:
            _emit_event(logs_dir, "auto_preempt", source="broker", **rec)
        for jid, proj, text in sweep_warning_records:
            _emit_event(
                logs_dir,
                "sweep_warning",
                source="broker",
                job_id=jid,
                project=proj,
                warning_text=text,
            )

    # Retention prunes (own sessions). Rows first — that path deletes a row AND
    # its log together — then the log-only pass over whatever rows remain.
    # Separate clocks: JOBD_JOB_RETENTION_DAYS (rows, default off) and
    # JOBD_LOG_RETENTION_DAYS (logs, default 60d). See constants.py.
    prune_old_jobs(session_local, logs_dir, now)
    prune_old_logs(session_local, logs_dir, now)
