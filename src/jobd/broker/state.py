"""Job state-machine transitions, dependency cascade, and heartbeat reconcile.

Helpers over a SQLAlchemy session + Job rows — no app/closure scope — so the
app factory and the sweeper can share the exact same transition rules. One
layering wart: `_reject_stale_worker` raises `fastapi.HTTPException` directly,
so this module isn't fully web-framework-free.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import or_, select

from jobd.broker.constants import (
    _DEPENDS_TERMINAL,
    _DEPENDS_TERMINAL_ANY,
    _FAILED_SIDE_TERMINAL,
    RECONCILE_MIN_AGE_SECONDS,
    RECONCILE_MISS_THRESHOLD,
)
from jobd.broker.events import _emit_event
from jobd.db import Job
from jobd.models import JobRequires, JobState


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


def _cas_state(session, job_id: int, expected: tuple[JobState, ...], **values) -> int:
    """Compare-and-swap a job's state: apply `values` only if the row is still
    in one of `expected`. Returns the affected rowcount — 1 = we won, 0 = the
    state changed between the caller's read and this write (another request, or
    the matcher's atomic queued->assigned claim).

    Mirrors that claim (next_job) so every transition write is guarded, closing
    the read-check-write TOCTOU: without it a `cancel` could overwrite a
    concurrent claim to CANCELLED without setting `signal`, and the worker would
    then run to completion a job the user was told was cancelled; likewise a
    late `/complete` could clobber a sweeper-set ORPHANED (and vice versa)."""
    result = session.execute(
        Job.__table__.update()  # type: ignore[attr-defined]  # SQLAlchemy: Table.update on __table__
        .where(Job.id == job_id, Job.state.in_([s.value for s in expected]))
        .values(**values)
    )
    return int(result.rowcount)


def _requeue_or_honor_cancel(
    session,
    job_id: int,
    expected: tuple[JobState, ...],
    *,
    now: datetime,
    reset_queue_clock: bool = True,
    **requeue_values,
) -> str:
    """Tear down a claim: requeue the job — unless a user cancel is pending.

    Every requeue site clears `signal` (M1) so a stale preempt can't kill the
    re-run. But a pending *cancel* is durable user intent, not an
    incarnation-scoped artifact: erasing it re-runs — possibly on another
    host — a job the user was already told was being cancelled (audit
    2026-07-05 A2). Honor it instead: the workload never started (ASSIGNED)
    or is being torn down anyway, so CANCELLED is the truthful outcome.

    Both writes qualify the WHERE on `signal` as well as `state`, so a cancel
    landing between the caller's read and this write can't be dropped either.

    Returns "cancelled" (caller must refresh, cascade, and emit job_cancelled
    after its commit — a cancel is a failed-side terminal), "requeued" (normal
    requeue; any non-cancel signal cleared), or "lost" (the row left
    `expected` under us — caller must not touch it)."""
    if _honor_pending_cancel(session, job_id, expected, now=now):
        return "cancelled"
    # M-1 (audit 2026-07-10): reset the scheduling_timeout queue-clock on
    # requeue, so a job that dispatched, ran for hours, then got reclaimed on a
    # worker death is not immediately killed by a scheduling_timeout measured
    # from its original submit. submitted_at is left untouched (it orders the
    # queue). audit 2026-07-12: but NOT on a refuse-admission requeue — that
    # job never ran and is being RE-ROUTED, which is exactly the "unscheduled"
    # time scheduling_timeout is meant to bound. Resetting the clock there lets
    # a job oscillating on gpu_contention (refusal doesn't grow the exclusion
    # set) evade scheduling_timeout forever, so those callers pass
    # reset_queue_clock=False.
    values: dict = {"signal": None, **requeue_values}
    if reset_queue_clock:
        values["last_enqueued_at"] = now
    result = session.execute(
        Job.__table__.update()  # type: ignore[attr-defined]  # SQLAlchemy: Table.update on __table__
        .where(
            Job.id == job_id,
            Job.state.in_([s.value for s in expected]),
            or_(Job.signal.is_(None), Job.signal != "cancel"),
        )
        .values(**values)
    )
    return "requeued" if int(result.rowcount) else "lost"


def _honor_pending_cancel(
    session, job_id: int, expected: tuple[JobState, ...], *, now: datetime
) -> int:
    """Signal-qualified CAS: transition the job to CANCELLED iff it is still in
    one of `expected` AND a user 'cancel' signal is pending. Returns rowcount
    (1 = cancel honored; the caller must refresh, cascade, and emit
    job_cancelled after its commit). Used wherever a claim is being torn down
    and a pending cancel makes the teardown disposition moot (A2)."""
    result = session.execute(
        Job.__table__.update()  # type: ignore[attr-defined]  # SQLAlchemy: Table.update on __table__
        .where(
            Job.id == job_id,
            Job.state.in_([s.value for s in expected]),
            Job.signal == "cancel",
        )
        .values(
            state=JobState.CANCELLED.value,
            finished_at=now,
            worker=None,
            started_at=None,
            signal=None,
        )
    )
    return int(result.rowcount)


def _reject_stale_worker(
    job_worker: str | None, reporting: str | None, job_id: int, action: str
) -> None:
    """Raise 409 if a worker other than the job's current owner is reporting.

    A job reclaimed after a partition (sweeper/reconcile requeue) and
    re-dispatched to a fresh worker can still receive /log, /started, or
    /complete from the ORIGINAL worker, which kept running the workload.
    Accepting those would clobber the current run's record/log or terminal-ize
    the re-dispatched job under the wrong worker (M2 double-execution). The
    reporting worker is carried in the `X-Jobd-Worker` header; `reporting is
    None` means a pre-header worker binary — skip the check (backward
    compatible), preserving the old best-effort behavior for old workers."""
    if reporting is not None and reporting != job_worker:
        raise HTTPException(
            status_code=409,
            detail=(
                f"job {job_id} is owned by worker {job_worker!r}, not {reporting!r}; "
                f"refusing stale {action}"
            ),
        )


def _cascade_on_parent_terminal(session, parent: Job) -> list[tuple[int, str]]:
    """When `parent` reaches a failed-side terminal state, transitively cancel
    the QUEUED descendants that did not opt into depends_on_any_exit. Returns
    (child_id, project) for every cancelled job so the caller can emit
    `job_cancelled` events (by='cascade', schema-v2) AFTER its commit.

    Transitive: a cancelled child is itself a failed-side terminal, so its own
    default-policy children must cascade too. In A<-B<-C, A failing cancels B,
    and B's cancellation must cancel C — the old single-level pass stranded C in
    QUEUED forever. Fan-in is handled by the depends_on membership test: a
    non-any-exit child needs ALL parents COMPLETED, so any one failing cancels
    it. Diamonds are safe — each job is cancelled at most once (`processed`)."""
    if parent.state not in _FAILED_SIDE_TERMINAL:
        return []
    # SQLite stores datetimes as naive UTC; keep finished_at/warning_at naive.
    now = datetime.now(UTC).replace(tzinfo=None)
    # Index the currently-QUEUED default-policy children by every parent id they
    # depend on. Built once from a snapshot: the traversal only cancels rows
    # (shrinking the live QUEUED set), never adds, so the snapshot stays a valid
    # superset and no row is re-queried mid-cascade.
    candidates = (
        session.execute(select(Job).where(Job.state == JobState.QUEUED.value)).scalars().all()
    )
    children_by_parent: dict[int, list[Job]] = {}
    for child in candidates:
        if child.depends_on_any_exit:
            continue
        for dep_id in json.loads(child.depends_on_json or "[]"):
            children_by_parent.setdefault(dep_id, []).append(child)
    cancelled: list[tuple[int, str]] = []
    processed: set[int] = set()
    # (parent_id, parent_state) frontier — the state annotates each child's
    # warning so it names the actual failed ancestor.
    frontier: list[tuple[int, object]] = [(parent.id, parent.state)]
    while frontier:
        pid, pstate = frontier.pop()
        for child in children_by_parent.get(pid, []):
            if child.id in processed:
                continue
            processed.add(child.id)
            child.state = JobState.CANCELLED.value
            child.finished_at = now
            child.warning = f"parent_failed: {pid} → {pstate}"
            child.warning_at = now
            cancelled.append((child.id, child.project))
            # The cancelled child is now a failed-side terminal; cascade to its
            # own default-policy children.
            frontier.append((child.id, JobState.CANCELLED.value))
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
) -> tuple[
    list[tuple[int, str]],
    list[tuple[int, str, list[tuple[int, str]]]],
    list[int],
    list[tuple[int, str, str]],
]:
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
    (orphan_records, cascade_records, requeued_ids, cancelled_records).
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    orphan_records: list[tuple[int, str]] = []
    cascade_records: list[tuple[int, str, list[tuple[int, str]]]] = []
    requeued: list[int] = []
    cancelled_records: list[tuple[int, str, str]] = []
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
        # CAS-guarded on the state we read (ASSIGNED or RUNNING) so a /complete
        # from the just-revived worker — which lands on its own session right as
        # this reconcile decides the worker is gone — isn't clobbered (F1). If
        # the row already left that state, skip it (don't requeue/orphan/emit).
        cur = JobState(j.state)
        if cur == JobState.ASSIGNED or idempotent:
            # M1: clear a stale preempt signal so the fresh worker this requeue
            # re-dispatches to doesn't honor it and kill the re-run — but a
            # pending user CANCEL is honored, not erased (A2).
            outcome = _requeue_or_honor_cancel(
                session,
                j.id,
                (cur,),
                now=now,
                state=JobState.QUEUED.value,
                worker=None,
                started_at=None,
                reconcile_misses=0,
            )
            if outcome == "requeued":
                requeued.append(j.id)
            elif outcome == "cancelled":
                session.refresh(j)  # sync ORM state so the cascade sees CANCELLED
                cascaded = _cascade_on_parent_terminal(session, j)
                if cascaded:
                    cascade_records.append((j.id, j.state, cascaded))
                cancelled_records.append((j.id, j.project, cur.value))
        else:
            if _cas_state(
                session,
                j.id,
                (cur,),
                state=JobState.ORPHANED.value,
                finished_at=now,
                termination_reason="worker_restarted",
                signal=None,
            ):
                session.refresh(j)  # sync ORM state so the cascade sees ORPHANED
                cascaded = _cascade_on_parent_terminal(session, j)
                cascade_records.append((j.id, j.state, cascaded))
                orphan_records.append((j.id, j.project))
    return orphan_records, cascade_records, requeued, cancelled_records


def _excluded_workers(job: Job) -> list[str]:
    """Hosts that refused this job for a missing cwd (back-compat: NULL/"[]" -> [])."""
    return json.loads(job.excluded_workers_json or "[]")
