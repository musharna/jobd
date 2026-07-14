# Architecture

How jobd is put together, and — more usefully — **why**. Most of the surprising decisions
here are scar tissue: something broke in production, and the shape of the code changed to
make that class of failure impossible rather than merely unlikely. Those are the parts
worth reading before you change anything.

For operations see [runbook.md](runbook.md); for the trust model see
[security.md](security.md); for preemption see [preemption.md](preemption.md).

---

## The shape of the thing

```
        submit                          claim (long-poll)
  CLI ─────────┐                    ┌───────────────────── worker (desktop)
  MCP ─────────┼──▶  BROKER  ◀──────┼───────────────────── worker (gt76)
  HTTP ────────┘     (FastAPI)      └───────────────────── worker (laptop, msi-4080)
                        │
                     SQLite            workers PULL. the broker never
                   (the truth)         connects out to a worker.
```

**Workers pull; the broker never pushes.** A worker long-polls `POST /next-job`, and the
broker holds the request open until something is dispatchable or the timeout expires.
This is the single most load-bearing decision in the design: workers live on laptops and
desktops behind NAT, they sleep, they lose wifi, they get closed mid-job. A broker that
had to *reach* them would need every one of them addressable and awake. Instead a worker
that vanishes simply stops asking, and the sweeper reclaims what it was holding.

**SQLite is the only truth.** No queue broker, no Redis, no in-memory state that matters.
Every dispatch decision is a compare-and-swap against a row. Restarting the broker loses
nothing: `queued` and `assigned` jobs are untouched, and `running` jobs survive because
their worker keeps streaming output and posts `/complete` when the broker returns.

---

## Layout

| module | what it is |
|---|---|
| `app.py` | HTTP surface. Routes, wiring, the dispatch long-poll, the sweeper loop. |
| `broker/submit.py` | Submission as a service: validation, effective-config cascade, array fan-out, persistence, the depends_on cascade. |
| `broker/admission.py` | The admission decision tree: what to do when a worker refuses a job at dispatch. |
| `broker/state.py` | The state machine. `_cas_state`, dependency resolution, cascade-on-terminal. |
| `broker/sweeper.py` | The reconciler. Dead workers, wall-clock kills, retention, queue warnings. |
| `matcher.py` | Pure functions: does this job fit this worker, and why not. No I/O, no DB. |
| `worker/job_worker.py` | The worker daemon: poll, launch, stream, reap, heartbeat. |
| `client.py` | The HTTP client both the CLI and MCP wrap. One place that talks to the broker. |
| `mcp/` | The MCP tool surface for agents. |

`app.py` was 1,873 lines and is now 1,385: `submit` and `refuse_admission` were the two
handlers holding real business logic, and they moved out. The remaining routes are thin.

---

## The state machine

```
                      ┌──────────── cancelled ◀── user, or a cascade from a failed parent
                      │
  queued ──▶ assigned ──▶ running ──▶ completed
     │           │           │    └──▶ failed
     │           │           └──────▶ preempted     (SIGTERM + grace, for checkpointing jobs)
     │           │           └──────▶ orphaned      (its worker died mid-run)
     │           └──────────────────▶ queued        (refused at admission, or worker died)
     └──────────────────────────────▶ scheduling_timeout
```

**Every transition is a compare-and-swap on the state it expects to find.** Not because
concurrency is theoretically hard, but because a worker that is briefly presumed dead can
come back and post `/complete` while the sweeper is mid-reclaim. Without the CAS, the
reclaim clobbers a completed job back to `orphaned` and the user is told their finished
work died. The guard is `WHERE state = 'assigned'` on the UPDATE, and `rowcount == 0`
means someone else won and we must not proceed.

**Dependencies cascade on failure, not just success.** If a parent reaches a failed-side
terminal state, its default-policy children can never dispatch — nothing will ever satisfy
them — so they are cancelled, not left to sit in `queued` forever. Getting this right took
two attempts; see *H-1* below.

---

## Three bugs that shaped the code

These are here because each one is a *class* of mistake that the design now prevents, and
because each was invisible until it wasn't.

### H-1: the fix that was a no-op while its test stayed green

A job could be submitted depending on a parent that failed *during the submit*. The
validation read the parent's state, found it non-terminal, and inserted the child; the
parent's cascade had already swept the queue and not seen it. The child stranded in
`queued` forever.

The fix — re-run the cascade after the insert — **did nothing in production**, because the
re-check read the parent through SQLAlchemy's identity map. With `expire_on_commit=False`
the intervening commit did not refresh it, so the cascade gate saw the *stale*
pre-race state and never fired. The test passed throughout.

`session.refresh(parent)` is the entire fix (`broker/submit.py`). **Do not remove it.**
The regression test injects the race by monkeypatching `_serialization_warning` — which is
why that name must stay in the module `submit_job` reads it from. If the code moves and
the patch site doesn't, the test passes while testing nothing, which is exactly how this
bug survived the first time.

### The dedup keys that could not discriminate

Two separate storms, one mechanism: **a guard whose key cannot capture what it must
distinguish.**

- `dispatch_skip` was keyed on `job_id` alone — but the skip *reason* is computed per
  worker, so four workers overwrote each other's answers and the "has it changed?" check
  was true on every poll. **54,118 events to serve 139 dispatches.**
- The blocked-queue warning embedded `queue-age 47m` **in the string used as its own dedup
  key**, so it re-emitted (and rewrote its DB row) every minute, forever.

The rule that falls out: **never put a self-ticking value in a key, and make sure the key
contains every dimension the value depends on.** Both are guarded by tests that fail if
the key regresses. A third instance — a container healthcheck that TCP-connected to a port
and therefore could not tell *which daemon* answered — is why `scripts/healthcheck.py`
makes a real authenticated request and validates the response body.

### Two walls, and only one of them opened

`/livez` and `/readyz` are unauthenticated so an external monitor can reach them. They
shipped exempt from the bearer token — and were still `403`, because the broker has a
**second** wall: a tailnet source-IP ACL. Every unit test passed, because the TestClient's
source IP is allow-listed and the ACL never engages in the suite.

`auth._PUBLIC_PROBE_PATHS` is now the single list consulted by *both* walls. Opening one
and forgetting the other is no longer expressible.

---

## Invariants worth not breaking

**Events are emitted only after the commit that justifies them.** A failed commit must
never leave `events.jsonl` claiming a cancellation the database never recorded. This is
why the emit loops sit *below* `session.commit()` and read from captured scalars rather
than reloaded ORM objects.

**The broker binds `JOBD_HOST`, never `0.0.0.0`.** With `network_mode: host`, the bind
address *is* the access-control boundary — Docker's userland proxy would otherwise NAT
every cross-host source IP to the bridge gateway and silently defeat the ACL.
`tests/test_deploy_lint.py` fails the build if this regresses, because one typo here
exposes remote code execution across the fleet.

**Coverage is derived, never remembered.** Three separate guards work this way, and they
exist because the same thing kept happening: something is added on one surface, forgotten
on another, and the test that should have caught it was itself written by hand and so
forgot too.

- MCP submit forwards fields derived from `JobSubmit.model_fields` minus a *documented*
  deny-list. `scheduling_timeout_s` was silently dropped for several releases because the
  old guard compared two hand-written lists — and forgetting a field updated both.
- MCP tool coverage is checked against the live FastAPI route table: a new route must be
  exposed or explicitly excused **with a reason**.
- The unauthenticated allow-list is checked against the live route table too: every other
  route must still 401.

In each case the point is the same. **Forgetting is what fails.**

---

## Things deliberately not done

Recorded because "why isn't there an X" is a fair question, and the answer is usually
"we measured".

- **No composite indexes.** Measured against the live 2,947-row database: the retention
  prune runs in 1.5 ms, a project+state job list in 1.0 ms, the dispatch scan in 0.005 ms
  — all already index-backed. A composite would add write cost to *every* job transition
  to save under a millisecond.
- **No wake-coalescing.** The dispatcher broadcasts a wake to all parked workers, and all
  but one lose the race. At four workers that costs nothing measurable, and coalescing
  risks dispatch *stalls* — a worse failure than a wasted query.
- **No retry/backoff queue.** A failed job stays failed. Retry policy is the caller's, and
  a broker that silently re-runs your job is a broker that runs it twice.
