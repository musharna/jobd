"""Broker tuning constants and derived terminal-state sets.

A leaf module (imports only jobd.models) so every other broker submodule and the
app factory can share these without an import cycle.
"""

from __future__ import annotations

import re

from jobd.models import TERMINAL_FAIL_STATES, TERMINAL_STATES, JobState

DEAD_WORKER_SECONDS = 300  # 5 min
IDEMPOTENT_RECLAIM_SECONDS = 90
OFFLINE_AFTER_SECONDS = 120
# Non-terminal states. A guarded /complete or /started transition wins only
# from one of these, so it can't clobber a concurrent terminal transition (e.g.
# a sweeper-set ORPHANED) — the complement of models.TERMINAL_STATES.
NON_TERMINAL_STATES: tuple[JobState, ...] = tuple(s for s in JobState if s not in TERMINAL_STATES)
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
WAIT_STREAM_CHUNK_BYTES = 64 * 1024  # /wait reads the log in 64 KiB slices to bound memory

_UNMATCHEABLE_WARNING_PREFIX = "no matching worker —"
_BLOCKED_WARNING_PREFIX = "queue-age "
_AUTO_PREEMPT_WARNING_PREFIX = "auto-preempted in favor of job "

_SINCE_RELATIVE_RE = re.compile(r"^(\d+)([hdw])$")

# depends_on terminal-state policy (see broker.state._deps_satisfied):
#   default policy waits for COMPLETED; any-exit unblocks on any terminal.
# Derived from the models.py sets (the promised single source of truth) rather
# than re-enumerated — a hand-copied set here is exactly the drift that would
# let a new terminal state silently miss the dependency cascade and strand
# children in QUEUED (the H2 bug class; audit 2026-07-05 A4).
_DEPENDS_TERMINAL = frozenset({JobState.COMPLETED.value})
_DEPENDS_TERMINAL_ANY = frozenset(s.value for s in TERMINAL_STATES)
# Failed-side terminal states: a parent here can never produce the output a
# default-policy (non-any-exit) child needs, so the child is cascade-cancelled.
_FAILED_SIDE_TERMINAL = frozenset(s.value for s in TERMINAL_FAIL_STATES)

# Server-side cap on GET /jobs `limit` (audit 2026-07-12). The endpoint used to
# return every row ever; `limit` is opt-in (None = all, so `graph`/`--array`
# still see the complete set) but a caller may not ask for an unbounded page
# beyond this.
LIST_LIMIT_MAX = 1000
