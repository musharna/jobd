# Worker SIGTERM drain

Status: design approved-pending-review · Audit finding: critical #2 (2026-06-10 audit)

## Problem

The worker's signal handler (`job_worker.py` `shutdown()`) calls
`_post_event(client, ...)` then `sys.exit(0)`:

1. **Deadlock risk.** The handler runs in the main thread, which may be inside
   an `httpx` call on the same `client` (long-poll, log POST). httpx's
   connection pool is lock-guarded; re-entering from the handler can deadlock
   on the lock the interrupted frame holds. The worker then hangs until
   systemd's stop timeout SIGKILLs it.
2. **Stranded jobs.** Job threads are `daemon=True`, so `sys.exit(0)` kills
   them without running `finally` blocks: the workload is never signaled, the
   cgroup-walk reaper never runs, `/complete` is never posted. The workload
   keeps running as an orphan.
3. **Permanently stuck RUNNING rows.** `Restart=on-failure` + `RestartSec=10`
   restarts the worker within seconds. The new process heartbeats under the
   same host, refreshing `Worker.last_heartbeat`, so the broker's
   dead-worker reaper (`DEAD_WORKER_SECONDS=300`, keyed on heartbeat age)
   never fires. The stranded job stays RUNNING in the DB indefinitely while
   its orphaned subprocess burns GPU/RAM.

A graceful drain fixes (1) and (2). (3) needs a broker-side backstop too,
because SIGKILL / OOM / power loss never run a drain.

## Constraints discovered in the current code

- **Long-poll latency.** `/next-job` holds the main thread for up to
  `POLL_TIMEOUT_S + 5 = 35 s`. A flag-only signal handler returns into the
  blocked syscall, which Python retries (PEP 475) — drain start is delayed by
  up to ~35 s. Interrupting via an exception raised from the handler is
  rejected: the exception can land between `_register_in_flight()` and
  `t.start()` in `_reserve_and_dispatch`, leaking a reservation (the exact
  race that function exists to prevent).
- **Inline execution at `max_concurrent=1`.** `_reserve_and_dispatch` runs the
  job _in the main thread_. While a job runs, the main thread is blocked in
  `proc.stdout.read(4096)` — a flag-only handler can't begin a drain until the
  job ends naturally.
- **Drain must flow through `run_job`'s signal machinery.** TERM-ing the
  workload directly makes the read loop see EOF with `got_signal=None`, so
  `rc=-15` lands as `final_state="failed"` and cascade-fails children. The
  drain must set `got_signal`/`termination_reason` first, like
  `poll_signals` does.
- **Preemption contract.** `final_state="preempted"` is terminal and NOT
  auto-requeued (docs/preemption.md); resume is operator-driven via
  `JOBD_CHECKPOINT_DIR`. A drain that preempts is consistent with the
  existing contract; checkpoint-token watching and `/checkpoint-complete`
  already work whenever `got_signal == "preempt"`.
- **systemd unit gaps.** `job-worker.service` has no `TimeoutStopSec`
  (default 90 s — too short for poll latency + grace) and default
  `KillMode=control-group`, which TERMs every process in the service cgroup
  at stop. Scope-wrapped workloads live in their own `jobd-<id>.scope` units
  and are unaffected, but **fast-path jobs are direct children inside the
  service cgroup** — systemd TERMs them at t=0, racing the drain and landing
  them as `failed` instead of `preempted`.

## Design

### Phase 1 — worker drain (fixes deadlock + stranded jobs)

**1. Flag-only signal handler.**

```python
def shutdown(_signum, _frame):
    stop_event.set()
```

No httpx, no `sys.exit`. SIGINT and SIGTERM share it. The main loop's
existing `while not stop_event.is_set()` gate ends the poll loop within one
long-poll round (≤ ~35 s); the drain runs after the loop, in the main
thread, where the shared client is safe to use.

**2. Always dispatch jobs in threads.** Remove the inline branch in
`_reserve_and_dispatch` (`max_concurrent > 1` check); single-slot workers get
the same thread path. The reservation invariant is unchanged (still
registered in the polling thread before `t.start()`). Threads stay
`daemon=True` — bounded shutdown comes from the drain deadline plus the
systemd KILL backstop, not from thread non-daemonness (a job thread wedged in
an unkillable wait must not block process exit forever).

**3. Per-job termination hooks.** Factor the body of `poll_signals`'
cancel/preempt branch into a closure inside `run_job`:

```python
def _initiate_termination(kind: str, reason: str | None, grace_s: float) -> None:
    # idempotent: first caller wins
    # sets got_signal["signal"]=kind, termination_reason["reason"]=reason,
    # _signal_workload("TERM"), schedules the SIGKILL-escalation Timer
```

`poll_signals` calls it for broker cancel/preempt and watchdog fires.
`run_job` registers `lambda reason, grace: _initiate_termination("preempt", reason, grace)`
into a module-level `_drain_hooks: dict[int, Callable]` (guarded by
`_in_flight_lock`, registered right after `Popen`, unregistered in the
finalize path next to `_tracked_pids_discard`).

**Gap race:** drain can fire between `_register_in_flight()` (poll thread)
and hook registration (job thread, post-Popen). Cover it from both sides: the
drain sets a module-level `_draining` flag _before_ invoking hooks, and
`run_job` checks `_draining` immediately after registering its hook and
self-initiates termination if set. Pre-Popen, `run_job` checks `_draining`
and refuses to even start the subprocess (posts `/complete` with
`final_state="preempted"`, `termination_reason="worker_shutdown"`,
`exit_code=None` — never ran).

**4. Drain procedure** (in `main()`, after the poll loop exits):

```
_draining = True
hooks = snapshot(_drain_hooks)
for hook in hooks: hook("worker_shutdown", grace)
deadline = monotonic() + JOBD_WORKER_DRAIN_GRACE_S (default 60) + 15
for t in job_threads: t.join(max(0, deadline - monotonic()))
post worker_shutdown event {drained: n_completed, aborted: n_still_alive}
client.close(); return (exit 0)
```

Per-job grace = `min(job's checkpoint_grace_s or 60, JOBD_WORKER_DRAIN_GRACE_S)`
— a drain must not inherit a 300 s checkpoint window unless the operator
raised the drain budget too. The existing run_job tail (KILL escalation,
cgroup-walk reap, `/checkpoint-complete`, `_post_complete_with_retry`) runs
unchanged inside each job thread; the join deadline gives it room. Threads
still alive at deadline are abandoned (logged + counted in the shutdown
event); Phase 2 reconciles their DB rows.

**5. Outcome semantics.** Drained jobs land exactly like a broker preempt:
`final_state="preempted"`, plus `termination_reason="worker_shutdown"` to
distinguish them. Not auto-requeued (consistent with docs/preemption.md);
checkpoint/resume convention applies. Workloads using
`install_preemption_handler` checkpoint normally.

**6. systemd unit changes** (`scripts/job-worker.service`):

```
KillMode=mixed          # TERM only the worker at stop; cgroup KILL at timeout
TimeoutStopSec=150      # 35 s poll latency + 60 s drain grace + margin
```

`KillMode=mixed` stops systemd from TERM-ing fast-path workloads at t=0
(which would race the drain and mislabel them `failed`). Document: raising
`JOBD_WORKER_DRAIN_GRACE_S` requires raising `TimeoutStopSec` in step.
Note: after a clean drain the worker exits 0, so `Restart=on-failure` does
not restart it on `systemctl stop` — correct.

### Phase 2 — broker reconcile backstop (fixes stuck-RUNNING for _all_ death modes)

Drain only covers graceful TERM. For SIGKILL/crash/power loss, close the
audit hole where a restarted worker's heartbeats keep the dead-worker reaper
from ever firing:

- **Worker:** include `in_flight_job_ids: [int]` in the heartbeat payload
  (from `_in_flight`; one lock acquisition in `resource_snapshot`). Old
  brokers drop the unknown field — backward compatible.
- **Broker:** in the sweeper, for each worker whose latest heartbeat
  _includes_ the field (None/absent ⇒ old worker, skip): any RUNNING or
  ASSIGNED job with `job.worker == host`, absent from the reported set in
  **two consecutive heartbeats** (debounce: covers the `/next-job` →
  first-heartbeat and `/complete`-in-flight races), and older than 60 s since
  assignment, gets the existing worker-died disposition: requeue if
  `idempotent`, else ORPHANED, `reason="worker_restarted"`. Reuses
  `orphan_records` / cascade plumbing from the RUNNING reaper.

### Phase 3 (optional hardening) — startup scope sweep

On worker start, enumerate user units matching `jobd-*.scope` and kill
leftovers from a previous incarnation, so a Phase-2-requeued idempotent job
can't double-execute against a still-running old scope. Assumes one worker
per user session (current deployment model — document it). Defer until
Phase 2 lands; without requeue-on-reconcile it has no double-execution to
prevent.

## Test plan

- **Unit (Phase 1):** handler body sets the flag and nothing else;
  `_initiate_termination` idempotence (second caller no-op); drain caps grace
  at `JOBD_WORKER_DRAIN_GRACE_S`; gap race (set `_draining` between
  reservation and hook registration → job self-terminates / refuses to start
  with `preempted`); drain joins threads and counts abandoned ones.
- **Integration (live marker):** real worker subprocess + sleep job; SIGTERM
  the worker; assert job reaches `preempted` with
  `termination_reason="worker_shutdown"` within the deadline, no surviving
  workload pid, worker exited 0.
- **Unit (Phase 2):** heartbeat with/without `in_flight_job_ids`; two-beat
  debounce; idempotent→requeued vs non-idempotent→ORPHANED with cascade.

## Rollout

Phases are independently shippable. Worker-first or broker-first deploys are
both safe (heartbeat field is additive; reconcile skips workers that don't
report it). Ship Phase 1 + unit changes together; Phase 2 next; Phase 3 with
or after Phase 2.
