# Changelog

All notable changes to jobd. Format roughly follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- **A cancel issued while a worker was frozen was silently dropped across an orphan→resurrect round trip (audit 2026-07-15 F1, HIGH).** The resurrect (v0.5.25) promises a user cancel is honored "through `GET /jobs/{id}/signal`" — but both worker-gone orphan writes (`worker_died` in the sweeper, `worker_restarted` in the reconcile) erased the pending signal with `signal=None`, so by resurrect time there was nothing left to poll. Exact scenario: worker freezes, user cancels the hung job, sweeper orphans it, worker thaws, job resurrects and **runs to completion** — durable user intent gone. Both writes now preserve the signal (inert on a terminal row if the worker never returns; a resurrected worker's next poll delivers the kill). A pending *preempt* survives the round trip too — a resurrect is the same incarnation, not a fresh one, so the M1 stale-signal hazard does not apply.
- **The un-cascade restored children with a stale queue clock (audit F5).** A child restored to QUEUED by a parent's resurrect kept its pre-outage `last_enqueued_at`, so one with `scheduling_timeout_s` shorter than the outage went straight back to SCHEDULING_TIMEOUT on the next sweep — the interval it spent *cancelled* counted as time "waiting to be scheduled". The restore CAS now stamps a fresh `last_enqueued_at`.
- **The dependency cascade's child write is now a CAS on QUEUED (audit F5).** It was a plain ORM assignment against a QUEUED snapshot; a user cancel landing in the window was overwritten with a `parent_failed` warning — same final state, but the stamp made the human's cancel wrongly restorable by a later parent resurrect's un-cascade. The CAS loses cleanly: the cancel stays untouched and unrestorable.
- **A resurrect flap on a stale in-flight report is pinned as self-healing (audit F4).** A heartbeat snapshot taken just before a worker finished a job can resurrect a row nothing is running; the forward reconcile re-orphans it within `RECONCILE_MISS_THRESHOLD` heartbeats. Behavior was already correct — now there is a regression test holding it.
- **The worker-CD idle gate read a heartbeat-lagged gauge, and its recovery comment was wrong (audit 2026-07-15 F2).** `busy()` consulted the `/workers` registry's `running` count, which only this worker's own heartbeat refreshes — a job claimed via long-poll right after a heartbeat was invisible to the gate for up to ~5s. The gate now counts **job rows** (`/jobs?state_filter=assigned|running`, owned by this host), which the `/next-job` claim writes synchronously. The comment claiming a lost race is fine because "the broker resurrects it on the next heartbeat" was false — a restart *drains*, and `worker_shutdown` is a deliberate terminal the resurrect never undoes; dependents cascade-cancel. The comment now states the real stakes.
- **`update-worker.sh`'s fail-safe branches were unreachable under `set -e` (audit L-2).** A broker-unreachable `$(api | python3)` assignment killed the script before its own diagnostic; the `${running:-1}` "unknown means busy" default could never engage. Both assignments now tolerate failure, so the written fallbacks actually run — proven by a real-execution test that stubs curl dead and asserts the diagnostic prints.
- **`install-worker.sh` installed unpinned PyPI latest — the exact violation `update-worker.sh` exists to prevent (audit L-1).** A fresh worker could start *ahead* of its broker, the schema authority. The installer now pins to the broker's own version (asked via `/health`) or an explicit `--version`, with no unpinned fallback, and its closing instructions install the update timer so the host self-heals thereafter.
- **A projects.yaml `defaults:` value now has to be sane before it can reach a Job row (audit 2026-07-15 F3).** YAML bypasses Pydantic, and the split was live: `max_wall_s: 0` reads as *unset* to the worker (`or`-falsy, never enforced) but *set* to the sweeper (`is not None`) — which then orphans the healthy running job as `wall_clock_exceeded`, a non-resurrectable terminal, and discards its `/complete`. A **string** value (`max_wall_s: "8h"`) TypeErrors the sweeper's arithmetic and aborts the ENTIRE sweep pass (scheduling timeouts, reclaims, offline marking, retention) every 30 seconds. `_parse_defaults` now enforces the same bounds as `JobSubmit` and drops bad values loudly. YAML `true` is caught too (`bool` is an `int` subclass — it would have landed as `max_wall_s=1`).
- **`dispatch_skip_state` eviction snapshots the dict before iterating (audit F6).** Concurrent `/next-job` attempts share it; iterate-while-insert raises "dictionary changed size during iteration" and 500s the poll.
- **The `/projects` mutation endpoints are serialized and the overrides overlay is written atomically (audit F7).** set/nudge mutate-then-iterate a shared dict from threadpool threads (dict-changed-size 500; last-write-wins overlay), and a crash mid-write could leave half a YAML document where the runtime priorities live. Lock + tmp-file + `os.replace`.
- **A typo'd `JOBD_LOG_LEVEL` no longer crash-loops the worker (audit L-3).** `setLevel("DEUBG")` raises `ValueError` at logging setup, and `Restart=on-failure` turns one bad env var into a crash loop. Unknown levels fall back to INFO with a warning.

### Security

- **Bearer tokens are no longer passed on curl's argv (audit 2026-07-15).** `-H "Authorization: Bearer $TOKEN"` is world-readable in `/proc/*/cmdline` for the lifetime of every timer-driven request. All scripts now pass the header via a `/dev/fd` curl config (`-K <(printf ...)`), and a lint keeps argv clean fleet-wide.
- **`deploy-broker.sh` validates its target before `sed`-ing it into the `.env` that holds the API token** — the target arrives from argv or the GitHub API, and anything but a plain `X.Y.Z` now dies before touching state. The `.env` is also `chmod 600`-enforced, and the health gate honors a configured `JOBD_PORT` instead of hardcoding `:8765` (a non-default port made the gate unpassable — and the deploy un-rollback-able — by construction).
- **The Prometheus event counter's `event` label is allowlisted (audit 2026-07-15 Sec-A).** `POST /events` takes free-form event names (hooks name their own events — by design), and `EVENTS_TOTAL.labels(event=...)` minted a **permanent time series per distinct name**: any token holder, or one buggy hook loop, could grow the broker's memory and every `/metrics` scrape without bound. Names outside the new `models.KNOWN_EVENTS` vocabulary now share one `event="other"` bucket (events.jsonl keeps the real name at full fidelity), `EventIngest.event` is length-bounded (64), and an AST sweep test proves every name emitted anywhere in the source is in the vocabulary — a broker event missing from it would silently alert-rate as "other".

### Changed

- The "is this job idempotent?" policy parse is now one helper (`broker.state._job_requires_idempotent`) instead of three pasted copies across the sweeper and reconcile (audit L-5); the dead `max_concurrent` parameter fell off `_reserve_and_dispatch` (audit L-6 — the real gate reads `len(_in_flight)` in the poll loop).

### Added

- **Real-execution tests for the CD pipeline (`tests/test_cd_scripts_execution.py`, audit T-LOW-2/3).** deploy-broker.sh runs against a stubbed `docker`/`curl` PATH: happy path (pull-before-pin ordering, pin written, metrics, 0600), **the rollback path** (health gate fails → previous pin restored → second `compose up` → exit 1) — logic that otherwise first executes during a failed production deploy — plus injection/`latest` refusal and DRY_RUN inertness. update-worker.sh: pinned dry-run, busy-defer on a real job row, other-hosts-ignored, and the unreachable-broker diagnostic. `healthcheck.py` is finally *executed*, not grepped: green on a real jobd payload, red on an alien daemon, an HTTP error, and a dead socket. The systemd `Environment=` lint now covers `*.timer` units too.

- **Worker CD (`scripts/update-worker.sh` + a 15-minute user timer) — the workers had no deployment story at all.** The broker self-deploys; the workers were upgraded only when a human remembered to restart them, so they drifted. That is not cosmetic: the `cwd_missing` pre-dispatch check shipped in **v0.5.10 on 2026-06-30 and did nothing for twelve days**, because the workers were still on 0.5.3/0.5.7. Nine jobs died of the exact fault it had been written to prevent, and CI was green the whole time. **A fix that never reaches a worker is not a fix.**

  The one dangerous power the updater has is `systemctl restart`, which SIGTERMs any in-flight job (60s drain, then `worker_shutdown`) — that is how 13 jobs died on 07-13/14, every one of them killed by a human upgrading a worker. So **it never restarts a busy worker**: it asks the broker (the only thing that knows) for its own `running` count and defers to the next tick, and re-checks *after* the install, because the worker long-polls and can claim a job during it. It targets the **broker's** version rather than PyPI latest — a worker must never run ahead of its broker, which is the schema authority. Guarded by mutation-verified lint (removing the gate, dropping the post-install re-check, and unpinning the version all fail CI), plus `bash -n` over every script in `scripts/` — because the text-matching lints would happily pass a file that is not a valid shell script at all, and did.

- **Fixed: the worker updater crashlooped on every host (exit 127), invisibly.** Two wrong assumptions, each caught only by *running* it — the text lints in `test_deploy_lint.py` read the script and never execute it, so both shipped green:
  1. **pip everywhere.** The install used `"$VENV/bin/pip"`, but the laptop's venv is uv-managed and has *no pip binary* — `command not found`, 127, on every tick.
  2. **uv everywhere.** The first fix switched to `uv`, but three of four hosts have a plain pip venv and *no uv installed at all* — so a uv-only install 127s on exactly the hosts pip worked on.

  The installer now **prefers the venv's own pip and falls back to uv** (resolved to an absolute path — `~/.local/bin` is not on the systemd user-service PATH), so it self-adapts to both venv shapes with no per-host config. Separately, a deploy helper had injected `Environment=JOBD_WORKER_VENV=$HOME/jobd-worker/.venv` into three update units; **systemd does not expand `$HOME`**, so the script looked for jobd under a literal `$HOME` path and found nothing (`installed=none`). Fixed to absolute paths and guarded: a new lint rejects any shell variable in a committed unit's `Environment=`, and a new lint requires both the pip and uv install paths to be present. **The lesson is real-execution testing: a guard that never engages the real environment cannot see a missing binary.**


## [0.5.25] — 2026-07-14

### Fixed

- **A live worker's job ran four hours past its own obituary, and its result was thrown away.** The laptop worker's heartbeat lapsed (a WSL freeze). The sweeper did exactly its job and orphaned the in-flight job as `worker_died`. But the worker had not died — it came back, and it had never stopped running the job. It then reported that job in `in_flight_job_ids` on **every heartbeat for three hours**, and the broker ignored it every time.

  `_reconcile_worker_in_flight` only ever queried rows it already believed were `ASSIGNED`/`RUNNING`. Terminal rows were outside its `WHERE` clause, so reported ids matching no such row were dropped on the floor. It answered *"the broker claims this is running — is it?"* and never the mirror question, *"the WORKER claims this is running — is it?"* Four hours of GPU folds ran to completion and the `/complete` was discarded by the terminal-is-terminal CAS; the ledger recorded a death at 17:56 while the GPU was at 99%.

  The reconcile now runs **both** directions. A job is resurrected only when the worker reports it in flight *now*, the broker has it as `ORPHANED` (never completed/failed/cancelled), the reason is one whose entire premise is "the worker is gone" (`worker_died`/`worker_restarted`), and it is still owned by that worker — never one re-dispatched elsewhere, which would put two live copies in flight. A user's cancel is deliberately **not** undoable: the worker learns of it via `GET /jobs/{id}/signal` and kills the workload.

  Resurrecting a parent also **undoes the cascade its orphaning caused** — otherwise the parent completes while the children cancelled *because it died* stay dead forever. `_cascade_on_parent_terminal` stamps each child with `parent_failed: {id} → {state}`, so its work is identifiable and invertible (a user's cancel has no such stamp and is untouched). Transitive, and a child with another genuinely-failed parent stays cancelled. Emits `job_resurrected` / `job_uncancelled`. Mutation-verified against 5 mutations, including restoring the original bug.


## [0.5.24] — 2026-07-14

### Fixed

- **`_default.defaults` was a fallback, not a floor — so configuring a project silently disarmed its zombie hang-guard.** `resolve_project_defaults` did `projects.get(name) or projects.get("_default")`, an either/or: a project *with* an entry never saw `_default.defaults` at all. That block is the fleet hang-guard — `idle_timeout_s` and `max_wall_s`, the reaper written after a silent job held a desktop GPU for six days — and `config/projects.yaml` described it as "the FLEET-WIDE hang-guard: any unlisted project inherits them". It reached exactly the projects nobody had configured, and fell off the moment anyone gave a project a priority: `job projects set NAME 70` writes a bare `{priority: 70}`, and that alone was enough.

  On 2026-07-14, registering 32 real projects to set their priorities disarmed the hang-guard on all 32 — including `orchid-sdxl`, the project whose 170-hour zombie *created* the guard. Nothing failed and no test went red; the guard was simply gone from everything that mattered, while the config file went on claiming it was fleet-wide.

  `_default.defaults` is now a **floor**: merged under every project, overridden one key at a time (setting `idle_timeout_s` no longer drops `max_wall_s`). The merge is derived from `dataclasses.fields`, so a field added later is inherited without anyone remembering to add it. `ProjectDefaults.escalate_to_arc` changes from `bool = False` to `bool | None = None`: every field now uses `None` as its unset sentinel, because a merge cannot otherwise tell "the project said nothing" from "the project said false" — and a project must be able to opt out of a floor. Guarded by `tests/test_project_defaults_floor.py`, mutation-verified against the old either/or.

- **`test_uv_lock_in_sync_with_pyproject` could only ever pass in CI, and the committed lock was stale through seven releases.** `uv run` syncs before it runs: handed a stale `uv.lock` it rewrites the file, *then* starts pytest. So the guard shelled out to `uv lock --check` against a lockfile its own runner had just repaired. `uv.lock` sat at `jobd 0.5.16` while `pyproject.toml` said `0.5.23`, with CI green the whole way; the staleness only surfaced running pytest directly. Fixed by setting `UV_NO_SYNC: "1"` once at the workflow level in `ci.yml` — not on individual `uv run` lines, because the next `uv run` someone adds would not have it — and re-locking. Verified both directions on a pristine checkout: with the rewrite a stale lock passes, with `UV_NO_SYNC` it fails as it always should have.

## [0.5.23] — 2026-07-14

### Fixed

- **The `/livez` and `/readyz` probes added in 0.5.22 cleared the bearer-token wall but not the tailnet ACL — and there are two walls.** They were exempt from the token and declared usable by external monitors; they were not. The blackbox exporter, a bridge container with source `172.20.0.4`, got `403 Forbidden`, so the probes were useless for the monitoring that was their entire justification. A probe that clears one of two walls is exactly as unreachable as one that clears neither.

  Every unit test passed, because the TestClient's source IP is allow-listed and the ACL never engages in the suite; only a real container revealed it. The tell was a contrast: Prometheus, on the *same* docker network, scrapes `/metrics` happily — because `/metrics` had been exempted from **both** walls.

  The fix is **one list consulted by both walls** (`_PUBLIC_PROBE_PATHS`), not a second parallel exemption — which would have re-created the same defect one refactor later. Safe by precedent and by content: `/metrics` is already ACL-exempt and exposes far more (job counts, worker versions, per-state gauges) than these two, which are mute. The listener is still bound to the tailscale IP, so reachability is unchanged; this only stops the ACL rejecting monitors that can already reach the port. Guarded by a test asserting a probe is reachable from a source that is neither tailnet nor loopback — because a monitor is, definitionally, not on the tailnet.


## [0.5.22] — 2026-07-14

### Added

- **`GET /livez` and `GET /readyz` — unauthenticated liveness and readiness probes.** Not a convention import: **Uptime Kuma monitors twelve homelab services and jobd was the only one it did not**, because every route sits behind a bearer token and a generic HTTP monitor cannot send one. The broker's log had zero 401s — nothing was even trying. `/livez` says the process is up and deliberately does not touch the database; `/readyz` round-trips a query to SQLite and returns 503 if it cannot. Two rather than one, because the distinction earns its keep when things break: `/livez` green with `/readyz` red is "the process is fine, the database is wedged" — fix the DB rather than restart the process, since restarting destroys the evidence.

  These are the **only** unauthenticated routes on the broker and they are deliberately mute — alive-or-not, ready-or-not, with no version, counts, or job data. The tailnet ACL still gates the source IP exactly as it does for the (also unauthenticated) `/metrics`, so the trust boundary is unchanged. `/health` keeps both its auth and its version, because the container healthcheck must *prove* it is talking to jobd and not to whatever else may be listening on the port. The exemption matches by **exact path, never a prefix**, and a derived guard enumerates the live route table to assert every other route still requires a token.

### Notes

- **Composite indexes were considered and rejected, on measurement rather than instinct.** Against the live 2,947-row database, the retention prune runs in 1.5 ms, a project+state job list in 1.0 ms, and the dispatch scan in 0.005 ms — all already index-backed by `ix_jobs_state`. A composite index would add write cost to *every* job transition to save under a millisecond on queries that run every 30 seconds or on user request.


## [0.5.21] — 2026-07-14

### Added

- **`jobd_events` MCP tool.** The broker's event stream is the only surface that explains *why* rather than *what*: `/jobs` says a job is queued; only the events say it has been skipped 400 times because no worker advertises `cuda-32gb`. It was reachable from the CLI and the HTTP API but not from MCP. Nine tools now.

### Fixed

- **The MCP error model had a hole an agent falls into.** `JobdClient` raises `BrokerRefusal` for *any* 4xx, but the rule table covered only 400/404/409 — so **401** (bad token), **403** (tailnet ACL) and **422** (malformed body) fell through to `kind: "unknown"` with the hint *"Unmapped broker 401: …"*. An LLM handed that has nothing to act on and, worse, no way to know the failure is *permanent*, leaving it free to retry a bad token forever. All three now map to a real kind whose hint says so explicitly. A 400 with an unrecognised message maps to `bad_request` for the same reason: the message may be novel, but "re-sending these arguments will not work" is known. Verified against a live broker — a genuinely bad token now returns `kind: auth_failed`.

- **The worker logged 41 diagnostics with `print(..., file=sys.stderr)`, and systemd filed every one of them as INFO.** A stderr stream gets a single journald priority (`SyslogLevel=info` by default), so `PRIORITY=6 (info) [worker] heartbeat error: [Errno 111] Connection refused` is what was actually in the journal. **`journalctl -p err` returned nothing while the worker was failing to reach the broker at all**, and no severity-based alert could ever have seen it. Now `log.info`/`warning`/`error` with levels chosen per message (8/10/23), and a formatter emitting systemd's `<N>` priority prefix — real journald priorities with no new dependency, and only when systemd is consuming the stream. Verified on a live worker: a genuine "cwd missing here; refusing admission" is now `PRIORITY=4 (warning)` and `journalctl -p warning` surfaces it.

### Changed

- **MCP↔broker surface parity is now DERIVED rather than remembered.** `tests/mcp/test_surface_parity.py` reads the route table off the live FastAPI app and asserts every route is either mapped to a tool or named in `_NOT_ON_MCP` **with the reason it should not be an agent action**. A route added tomorrow that nobody exposes now *fails a test* instead of quietly living on one surface — the recurring drift the 2026-07-12 audit kept finding. The converse is enforced too: a registered tool covering no route fails, because every tool costs model attention (a duplicate `jobd_job_get` was deleted in 0.5.16 for exactly that). Same shape as the `_SUBMIT_SYNTHESIZED` deny-list: coverage derived, exclusions named, **forgetting is what fails**.


## [0.5.20] — 2026-07-13

### Fixed

- **The blocked-warning was its own dedup key, and it carried a clock.** The sweeper re-emits a warning only when it differs from the one stored on the job (`if j.warning != new_w`) — but the blocked-warning rendered as `queue-age 47m: blocked by non-preemptible job N on ...`, and that `47m` ticks by itself. So the comparison was true on *every* sweep, forever: a job blocked behind a non-preemptible neighbour re-emitted a `sweep_warning` event **and rewrote its DB row every minute it stayed blocked**. In production that was 974 events over 7 days, one job accounting for 498 of them.

  The diagnosis is self-proving: the *unmatcheable* warning runs through the same dedup code three lines away, carries no clock, and deduped perfectly — one event, not 561. Identical mechanism, opposite outcome, and the only difference is the ticking value in the key.

  The queue age is not lost: it is derivable from `submitted_at`, and rendering it at read time is strictly *more* accurate than a number frozen at whenever the last sweep ran. What the warning is *for* is the blocker's identity and the remedy, and neither changes minute to minute. The prefix moves `"queue-age "` → `"blocked: "` accordingly.

  This is the third instance of one class — a guard whose key cannot capture what it must distinguish (see also the `dispatch_skip` dedup in 0.5.17, and the healthcheck in 0.5.17 that could not tell *which* daemon answered). A sweep of the whole event surface by repeat-count per (event, job) confirms there is no fourth: every other event fires at most twice for a given job, i.e. once per state transition.


## [0.5.19] — 2026-07-13

### Changed

- **Services, not routes.** The two fat route closures are now callable services: `jobd.broker.submit.submit_job` (~295 lines — validation, the effective-config cascade, array fan-out, persistence, the H-1 TOCTOU cascade sweep, event emission) and `jobd.broker.admission.refuse_admission` (~250 lines — the admission decision tree: stale-worker rejection, the ASSIGNED guard, pending-cancel honouring, and the requeue / terminal-fail / cwd-exclusion / auto-preempt branches). The routes are thin adapters. **`app.py`: 1,873 → 1,351 lines (−28%).** No behavior change.

  Worth recording *how* this was verified, because a passing suite could not have shown it. The tests guarding the two worst bugs this project has shipped work by monkeypatching module-level names **through the module that reads them** (`_serialization_warning`, `_FAILED_SIDE_TERMINAL`). Moving `submit` out of `jobd.app` breaks those patches — and the failure mode is not a red suite, it is **both tests continuing to pass while testing nothing**. That is exactly what the 2026-07-12 audit found: an H-1 fix that was a silent no-op in production while its test stayed green (0.5.13). So the refactor was gated on mutation instead: re-inject each historical bug and require the named test to *fail*, both before the move (proving the guards were alive) and after (proving they still are). `tests/unit/test_submit_service_guards.py` makes that permanent — the patch targets must demonstrably intercept, and `jobd.app` must not re-expose the stale names, since a direct assignment there would silently create an attribute nothing reads.

### Fixed

- **One safe way to build a job log path** (`py/path-injection`, high). Four places were composing `logs_dir / f"{job_id}.log"` by hand and had already drifted — two raw interpolations, one ad-hoc `int()` cast added to quiet this very warning, and a differently-named variable in the sweeper. The traversal was not reachable (FastAPI types `job_id` as `int`, so `../` is rejected with a 422 several frames earlier), but that made the safety a property of an annotation in another module and invisible at the filesystem call. `job_log_path()` is now the only way to name a job's log file, and coercion inside it makes the guarantee local and total.

## [0.5.18] — 2026-07-13

### Added

- **The broker image is now published to GHCR** (`ghcr.io/musharna/jobd:X.Y.Z`) on every `vX.Y.Z` tag, and `docker-compose.yml` pulls a **version-pinned** image instead of building in place.

  This removes the `--build` footgun rather than guarding against it. Compose used to have `build: .` + `image: jobd:latest` with **no registry behind that tag**, which meant `docker compose pull` was a silent no-op and a bare `docker compose up -d` after a `git pull` found a `jobd:latest` already present and reused it — restarting the *old* code, with nothing to say so. The mitigation was to remember `--build` forever. Now `pull` means something, the running version is knowable and pinned, and **rollback is possible at all** (you cannot roll back to a moving tag). Building from source is still supported, deliberately, via `docker-compose.build.yml`.

- **Pull-based continuous deployment** (`scripts/deploy-broker.sh` + `jobd-deploy.timer`). A timer on the broker host checks every 5 minutes for a newly published release and deploys it. Pull-based on purpose: the broker host has no public ingress, so push-based CD would mean storing a tailnet auth key and an SSH key in GitHub and letting CI reach into the network. **No secrets in GitHub, no inbound access**, and the check is nearly free when there is nothing to do.

  The deploy **verifies itself**, and each property is a bug this project has actually shipped: it pins an exact version rather than following `latest`; it confirms success by asking the *broker* its version rather than treating a clean `docker compose up` as proof the new code is serving; and it health-gates on `/health` rather than a socket connect. If the broker does not report the deployed version within 90s, **the previous pin is restored automatically**. Deploy success and the running version are exported as node-exporter textfile metrics, so a failed deploy is visible in Prometheus instead of only in a journal nobody reads. `DRY_RUN=1` reports what it would do and changes nothing.

  Rollback is now `sudo jobd-deploy 0.5.17`.

### Notes

- The GHCR package publishes **public**, so the broker host pulls it with no credentials at all — verified: the host has no `~/.docker/config.json` and ghcr.io issues an anonymous pull token for the image. Nothing in the deploy path holds a secret.

## [0.5.17] — 2026-07-13

### Fixed

- **The `dispatch_skip` event storm: 54,118 events to serve 139 dispatches.** Framed in the backlog as a thundering-herd problem, it was really a dedup-key defect. `explain_skip(jobs, w)` computes its reason against a *specific worker*, so one queued job legitimately has different reasons on different hosts — but the dedup state was keyed by `job_id` **alone** and shared across all workers. Each worker's answer overwrote the previous one, so the "has the reason changed?" guard was true on *every* poll and the event fired every time any worker polled. Two permanently-unplaceable jobs produced 3,908 of 5,340 skip events in one sample, each an append to `events.jsonl`. Now keyed on `(job_id, worker_host)`. The GC had the same shape of bug — `queued` is already filtered per-worker, so evicting entries for jobs merely invisible to *this* worker discarded other hosts' state and made them re-emit — and is now scoped to the polling worker.

- **The container healthcheck never probed the broker, and passed anyway.** It TCP-connected to a hardcoded `127.0.0.1`, but the broker runs `network_mode: host` and binds `$JOBD_HOST`; it has never listened on loopback. It reported green because an unrelated container held `127.0.0.1:8765` and **a bare TCP connect cannot tell which daemon accepted the socket**. Moving that container off the port exposed it: the probe began failing against a broker that was serving perfectly. It now makes an authenticated HTTP request to `$JOBD_HOST` and requires jobd's own `/health` payload back, so a squatting daemon fails the check rather than satisfying it.

- **N+1 dependency reads on every claim attempt** — one point-read per parent, per queued job, per attempt, per parked worker. Now a single batched `SELECT id, state ... WHERE id IN (...)`. `_deps_satisfied_bulk` is *fresher* than the loop it replaces, not merely cheaper: it bypasses the identity map, whereas `session.get()` under `expire_on_commit=False` can return a cached pre-commit state — precisely how the original H-1 cascade fix became a silent no-op (see 0.5.13). `_deps_satisfied` now delegates to it, so there is one copy of the terminal-state policy rather than two that can drift apart.

### Changed

- **The sweeper no longer wakes every parked worker every 30 seconds regardless of whether anything happened.** It broadcast a wake on every pass; each parked `/next-job` then ran a full claim attempt against an unchanged queue, and all but at most one returned nothing. It now wakes only when the sweep actually changed state. Safe by construction: parked workers re-attempt every `_LONGPOLL_RECHECK_S` (10s) as a backstop, so an over-narrow predicate costs bounded latency, never a stuck job.

### Notes

- Deliberately **not** done: a composite `(state, priority, submitted_at)` index, and a rewrite of the wake mechanism to wake exactly *K* workers. At a queue depth of 1–2 with four workers, the index would add write cost to every job transition to speed up a scan that is not slow, and coalescing would risk dispatch stalls to solve a fan-out that does not hurt at this scale. The storm was the pathology, and it is fixed at the cause.

## [0.5.16] — 2026-07-13

### Fixed

- **The MCP surface silently dropped `scheduling_timeout_s`.** It shipped on the broker and the CLI; jobs submitted through MCP could not be given a scheduling deadline, and nothing said so. The guard built to catch exactly this — a test named "round trips every JobSubmit field" — *could not fire*: it compared a hand-written passthrough tuple in `translate.py` against a hand-written expected set in the test, so both halves had to be remembered when `JobSubmit` grew a field, and neither was. The test only ever checked the fields someone had already remembered.

  The fix inverts the direction. The MCP submit surface is now **deny-listed, not allow-listed**: `_SUBMIT_PASSTHROUGH` is *derived* from `JobSubmit.model_fields` minus a documented `_SUBMIT_SYNTHESIZED` list, each entry carrying the reason it is synthesized rather than forwarded. A new broker field is now either forwarded automatically or it must be named with a justification — **forgetting is what fails**. Forwarding `scheduling_timeout_s` and collapsing `arch`/`os` into `requires` both fall out of that change rather than needing their own patches.
- **The systemd unit deployed stale code on boot.** `docker-compose.yml` pairs `build: .` with `image: jobd:latest` and there is no registry behind that tag, so `docker compose pull` is a no-op and `up -d` after a `git pull` re-runs the *old* image. `ExecStart` now passes `--build`; the runbook says why, and names the tell (a `job ping` version that disagrees with the checked-out tag).

### Added

- **Workers report their version in the heartbeat.** The fleet is upgraded host-by-host over SSH, so it routinely runs mixed versions — and the broker had no way to know: a worker three releases stale was indistinguishable from a current one. `Worker.version` (additive migration) now surfaces on `/workers` and as `jobd_worker_version_info{host,version}`. Paired with the existing `jobd_build_info`, drift becomes *alertable*, not merely visible:

  ```promql
  count(jobd_worker_version_info unless on(version) jobd_build_info) > 0   # a worker is adrift
  count(count by (version)(jobd_worker_version_info)) > 1                  # fleet is not uniform
  ```

  A worker too old to report reads as `null` / `version="unknown"` rather than inheriting the broker's version or vanishing from the series — the absence is itself the answer, and a missing series would shrink the drift count instead of raising it. The version is also **not** pinned to its last-known value on downgrade: a registry that makes the fleet look newer than it is would be worse than not knowing.

### Removed

- **`jobd_job_get` (MCP tool) and `client.job_get()`.** `jobd_job_get` was byte-identical to `jobd_status`'s no-wait path — both `GET /jobs/{id}` — while its description advertised fields it implied `jobd_status` lacked. It spent 1/9 of the MCP tool budget actively misdirecting tool selection. `jobd_status`'s description now states the scheduling internals it has always returned. Use `jobd_status` / `status()`.

## [0.5.15] — 2026-07-12

### Added

- **Split retention: job rows and job logs now prune on independent clocks.** Retention had been off entirely, and for a good reason — one shared clock forced a false choice. Measured on the live broker: 2,875 job **rows** = **6.4 MB** (~7 MB/yr), but 2,605 job **logs** = **2.0 GB** (~0.7 GB/month). A row is ~2 KB and feeds the ETA estimator's per-project p50/p90, so it is cheap and gets *more* useful with age; a log is ~800 KB and is write-once-read-maybe. Pruning them together meant discarding cheap, valuable history purely to reclaim expensive disk. Now:
  - **`JOBD_LOG_RETENTION_DAYS`** (default **60**) unlinks the `.log` of a terminal job finished more than N days ago while **keeping the row** — this is what bounds disk. Emits `logs_pruned`. `0` disables.
  - **`JOBD_JOB_RETENTION_DAYS`** (default **0**, unchanged) deletes the terminal row itself. Still opt-in, because rows are nearly free and the estimator wants the history.
- **`/jobs/{id}/output` reports `pruned`.** A pruned log and a never-written one both leave no file on disk, but they mean opposite things — reporting a pruned log as empty output would make a job that emitted megabytes look like it produced nothing. `job logs` now says the log was pruned by retention and points at `job status`.

### Changed

- Jobs carry a `log_pruned_at` stamp (additive migration), so the log-prune scan shrinks monotonically instead of re-stat'ing every historical log on every 30s sweep. A log whose unlink genuinely fails is left unstamped and retried.

## [0.5.14] — 2026-07-12

First batch from the 2026-07-12 improvement audit. Headline: `job projects set` / `nudge` were returning HTTP 500 in production — a documented feature was dead.

### Fixed

- **`job projects set` / `job projects nudge` returned HTTP 500 (config was mounted read-only).** `_persist_projects` rewrote `config/projects.yaml` in place, but that file is git-owned *and* bind-mounted `:ro` into the broker container, so every priority mutation raised `OSError: Read-only file system`. Verified against the live broker. Config ownership is now split: `config/projects.yaml` stays the **git-owned, read-only baseline** (projects, their baseline priority, and their `defaults:`), while runtime priority changes persist to a **writable overlay** at `$JOBD_STATE_DIR/project-priorities.yaml` (defaulting to the SQLite DB's directory, so it is covered by the DB backup and never conflicts with a `git merge` on redeploy). The overlay stores only *deltas* from the baseline, so a later config-as-code priority change still lands for any project nobody nudged. Because the endpoints only ever touch `priority`, `defaults:` blocks are now git-only — making the old "one nudge silently erases every `defaults:` block" round-trip hazard structurally impossible rather than merely tested against. See the new "Config vs state" section in `docs/runbook.md`.
- **Fleet-wide hang-guard `_default` defaults are now committed.** `idle_timeout_s: 3600` / `max_wall_s: 172800` existed only as an uncommitted edit on the broker host — one `git clone` away from being silently lost.

### Added

- **`GET /jobs` pagination.** The endpoint used to return *every row ever* — on a broker with retention off, the entire history. It now accepts `limit` (1..1000) and `offset`, and always reports the full filtered count in the `X-Total-Count` header. `limit` is deliberately opt-in (absent = all), because `graph` and `--array` build over the complete set and a silent default cap at the API layer would quietly corrupt them.
- **`job list --limit/-n` (default 50) and `--all`.** `job list` used to dump the broker's entire job history. It is now bounded by default and prints `… showing 50 of N` rather than truncating silently. `--array` still shows every member of an array (a truncated array misrepresents its shape). The MCP surface had already capped its own output; the human CLI had not.
- **`job --version` / `-V`**, and a bare `job` now prints the full help (with the command list) instead of a terse usage error. `JOBD_URL` and `JOBD_API_TOKEN` are documented in `job --help`, and the six commands that rendered as blank rows (`cancel`, `wait`, `classify`, `projects list/set/nudge`) now have help text.

### Changed

- **SQLAlchemy connection pool sized above the threadpools that feed it.** The engine used QueuePool's defaults (`pool_size=5` + `max_overflow=10` = 15 connections) while *two* threadpools open sessions against it — anyio's (40 tokens; every sync endpoint) and `asyncio.to_thread`'s (the `/next-job` dispatch scan). Past 15 concurrent DB-touching requests, the surplus threads blocked on pool checkout for up to `pool_timeout` (30s) — a cliff reached exactly when the dispatch fan-out wakes every parked worker at once. Now 20/60, tunable via `JOBD_DB_POOL_SIZE` / `JOBD_DB_MAX_OVERFLOW`.

### Removed

- **`test_full_suite_green`**, a meta-test that shelled out to re-run the entire suite as a subprocess. It contributed zero coverage, ignored the outer `-m`/`-k`/`-x` filters, doubled the cost of every new test, and its own docstring scoped it to a migration that finished when `mcp-v1` was tagged. CI already runs the full suite. **Suite runtime: ~136s → ~66s.**

## [0.5.13] — 2026-07-12

Fixes from the 2026-07-12 focused re-audit of the v0.5.12 delta. Headline: the v0.5.12 H-1 fix was a no-op in production and is now actually closed.

### Fixed

- **Submit `depends_on` TOCTOU fix was a no-op (H-1, regression of the v0.5.12 fix).** The v0.5.12 post-commit cascade re-check read the parent through a cached identity-map object; with `expire_on_commit=False` the intervening commit never refreshed it, so the cascade gate saw a stale non-terminal state and never fired — a child concurrently orphaned by its parent still stranded QUEUED forever. `/submit` now `session.refresh()`es each parent before the cascade re-check (matching the idiom every other `_cascade_on_parent_terminal` caller already uses), guarded for a parent pruned mid-submit. New real-race regression test drives the actual `/submit` endpoint with a concurrent-session `/complete` and fails on the pre-fix code.
- **`scheduling_timeout` queue-clock reset on refuse-admission re-routes (M-1 follow-up).** A refuse-admission requeue funnels through the same helper that resets `last_enqueued_at`, so a job that never ran but kept getting offered-and-refused (e.g. oscillating `gpu_contention`, which does not grow the exclusion set) reset its timeout clock every bounce and could evade `scheduling_timeout` forever. Refuse-admission re-routes now preserve the clock; only requeues of a job that actually dispatched reset it.
- **Worker `run_job` exception path leaked a signal-poll thread and a fast_path child (F1/F2).** On a raise past the terminal path, the outer `finally` now stops the signal-poll thread (it would otherwise spin on `GET /signal` forever for a terminal job) and SIGKILLs a `fast_path` child that has no scope cgroup to reap.

### CI

- **Property-based fuzzing of the broker state machine (H-4, audit 2026-07-10).** A `hypothesis` `RuleBasedStateMachine` (`tests/property/`) generates arbitrary interleavings of submit / claim / start / complete / cancel / worker-death-reclaim / scheduling-timeout / sweep against the real broker (in-process FastAPI + SQLite + sweeper) and asserts the invariants the CAS discipline guarantees: no terminal state is ever clobbered, a job is terminally cancelled at most once, a reclaim never silently drops a pending user cancel, and (added 2026-07-12) a default-policy dependent of a failed parent is never stranded on a runnable path. The harness now generates `depends_on` edges so it actually exercises the cascade (previously it created none, leaving the cancel-at-most-once invariant vacuous). Runs bounded in CI (`JOBD_H4_EXAMPLES` / `JOBD_H4_STEPS` env overrides for a deep run).
- **The `live` CI job silently skipped 5 of 9 marked-live tests (H-3 follow-up).** It exported only `JOBD_LIVE`, while the e2e / MCP / project-defaults live tests gate on `JOBD_E2E` / `RUN_LIVE_JOBD` and require an external broker — so they skipped, and skips don't fail pytest. The job now stands up a real broker + worker from the committed `config/`, exports all three opt-in gates, and guards against a broken gate silently running zero tests.

## [0.5.12] — 2026-07-10

Fixes from the 2026-07-10 v0.5.11 4-reviewer audit (security / broker-correctness / worker-reliability / hygiene).

### Fixed

- **Submit `depends_on` TOCTOU → permanent QUEUED strand (H-1).** The failed-side reject in `/submit` is a point read of `parent.state`; between it and the child INSERT+commit a parent could reach a failed-side terminal and run its cascade over a QUEUED set that didn't yet include the child, stranding a default-policy child QUEUED forever (no future transition re-fires the parent's cascade). `/submit` now re-runs the cascade for each parent after the child is committed and visible — a no-op unless the parent is already failed-side terminal.
- **`run_job` terminal path unguarded → strand + tracked-pid leak + un-reaped scope (H-2).** A `proc.stdout.read()` pipe/closed-file race or a signal-thread start failure raised past the terminal path; the outer dispatch handler only logged. The post-Popen body is now wrapped so a terminal `/complete(failed, worker_exec_error)` + tracked-pid discard + best-effort scope kill always run when the body doesn't reach its own terminal post.
- **Reclaimed long-running job killed by `scheduling_timeout` (M-1).** The sweeper keyed the deadline on `submitted_at`, which requeue paths copied forward — a job that dispatched, ran for hours, then had its worker die was immediately SCHEDULING_TIMEOUT'd and its dependents cascade-cancelled. A new `last_enqueued_at` column (additive migration) is reset on every requeue and now keys the timeout; `submitted_at` (which orders the queue) is untouched. NULL falls back to `submitted_at`.

### Security

- **Interactive docs disabled.** `/docs`, `/redoc`, and `/openapi.json` are Starlette-mounted and bypassed the app-level `require_token`, letting a tokenless tailnet peer enumerate the API. Now off (`docs_url`/`redoc_url`/`openapi_url` = `None`).

### Added

- **Prometheus `jobd_events_total{event,source}` counter (M-3).** The gauges were point-in-time; cumulative failure/throughput signals (cancellations, cascades, preempts, refusals) are now rate-alertable, incremented at the single `_emit_event` choke point.

### CI

- **The live worker↔broker suite gates merges (H-3).** A new `live` job (`JOBD_LIVE=1`) runs the real 2-process contract tests, previously deselected on every merge — the gap that let H-1/H-2 ship.

## [0.5.11] — 2026-07-05

### Changed

- **Quality batch from the 2026-07-05 audit (A4/A5 + LOWs).** (a) The dependency-cascade terminal sets in `broker/constants.py` are now _derived_ from the `models.py` single-source sets instead of hand-re-enumerated — a new terminal state can no longer silently miss the cascade (the H2 stranded-children class). (b) `build_app` no longer injects its test seams into module `globals()` (where the last-built app silently won module-wide and pinned its engine alive); they live on `app.state` (`sweep_once` / `prune_old_jobs` / `engine`) and all ~100 test references were updated. (c) `POST /jobs/{id}/complete` takes a typed `CompletePayload` (an untyped dict let a malformed worker payload store a non-int `exit_code`). (d) Dead `JobState.PREEMPT_REQUESTED` removed (nothing ever set it, but it sat inside `NON_TERMINAL_STATES`). (e) `jobd_workers{state="stale"}` is a fixed metrics series instead of appearing/disappearing. (f) CLI: `preempt-blockers` exits 3 (not 2, which means broker-unreachable) for the benign "no blocker signaled" outcome; the exit-code map is documented in `main()`; `list` loses its `-w` short flag (`-w` means wait/watch elsewhere); `cancel`/`workers`/`gpu-holders` use the `_client()` helper. (g) Dead `logs_dir` param dropped from `_cascade_on_parent_terminal`/`_reconcile_worker_in_flight`; stale docstrings (app.py "split at 500 lines" header, missing `sweeper` in the broker package map, `sweep_once` one-liner) and false "string value (not the enum)" StrEnum comments corrected.

### Fixed

- **Preempt-signal writes are now CAS-guarded, and the `/next-job` claim clears any leftover signal (audit 2026-07-05, A1).** The sweeper's auto-preempt and `POST /jobs/{id}/preempt-blockers` stamped `signal='preempt'` with plain ORM writes — no guard on the state the candidate was selected in — so a `/complete` or reconcile-requeue landing in the window could carry a stale preempt onto a requeued job; and since the atomic queued→assigned claim never touched `signal`, the fresh worker would read it on its first poll and kill the brand-new run seconds in. Both writers now CAS on `(assigned, running)` (a lost race returns `signaled: null` from `/preempt-blockers` instead of a false success), and the claim UPDATE clears `signal`, killing the stale-signal class outright.
- **A pending user cancel is honored, not erased, when a claim is torn down (audit 2026-07-05, A2).** Cancelling an `assigned` job sets `signal='cancel'` and tells the user "cancelling" — but every requeue path (admission refusal ×2, sweeper dead-worker reclaims, heartbeat reconcile) cleared `signal` unconditionally (the M1 fix, aimed at stale _preempt_ signals), so the cancel vanished and the job silently re-ran, possibly on another host. Teardown now goes through a signal-qualified CAS (`_requeue_or_honor_cancel`): a pending cancel transitions the job to `cancelled` (with dependency cascade + `job_cancelled` event, `via=refuse_admission|sweeper_reclaim|reconcile`), while non-cancel signals are still cleared on requeue. `/refuse-admission` also checks first, so a cancelled job can't be mislabeled `failed/cwd_unreachable` on the no-eligible-worker branch.
- **`cwd_unreachable` failures now stamp `finished_at` (audit 2026-07-05, F-3).** The terminal transition omitted it, and retention pruning filters on `finished_at` — so these rows (and their logs) were never pruned. Also clears any pending signal, matching `/complete`.
- **Sweeper phase-2 events (`auto_preempt`, `sweep_warning`) are emitted only after their commit (audit 2026-07-05, F-4).** They fired mid-transaction, so a failed commit could leave `events.jsonl` claiming a preempt signal or warning the DB never recorded — the same emit-after-commit invariant the M3 fix established for phase 1.
- **Worker offline/stale marking re-checks `last_heartbeat` in the write (audit 2026-07-05, F-7).** The sweeper's SELECT-then-set could clobber a heartbeat that committed in between, briefly taking a live worker out of matching and emitting a spurious `worker_offline`/`worker_stale` event.
- **Heartbeat reconcile wakes long-pollers when it orphans or cancels jobs (audit 2026-07-05, F-6).** It only woke on requeues; orphans/cancels are failed-side terminals that can unblock `depends_on_any_exit` dependents, which previously waited out the ~10s recheck backstop.
- **Concurrent `/next-job` attempts can no longer 500 on the skip-dedup cleanup (audit 2026-07-05, F-5).** Two attempts share the dedup dict; `del` raced (`KeyError`) — now `pop(…, None)`.
- **`POST /events` returns 422 (not 500) when a payload key collides with an envelope field (audit 2026-07-05).** A payload containing `source`/`job_id`/`project`/`event`/`ts` blew up the emit call with `TypeError: got multiple values for keyword argument`; it's now rejected at the Pydantic boundary (which also closes the only route to forging envelope fields).
- **MCP `jobd_list` now does what its schema says (audit 2026-07-05, A6).** The advertised `limit` (default 50) was never applied anywhere — `GET /jobs` has no window, so on a long-lived broker with retention off (the default) a bare `jobd_list` dumped the entire job history into the model's context — and the documented state default (`queued/assigned/running`) wasn't implemented either (all states returned when omitted; only the _first_ element applied when a multi-state filter was passed). Omitting `state` now returns the active set (explicit `[]` opts into all states), multi-state filters apply client-side, and `limit` caps the jobs array to the newest N (clamped `[1,200]`) with `counts` still covering the full filtered set and a `truncated` field reporting the overflow.
- **MCP error mapping matches the broker's real detail strings (audit 2026-07-05, A7).** Two rules were dead — "unknown parent job" and "no eligible worker" match nothing the broker ever emits — while the most common submit hard-fails (the matcher's mount-root cwd rejections) fell through to kind `"unknown"` with no hint. The missing-parent rule now matches the actual `depends_on refers to missing job: N`, a new `parent_failed` rule covers the terminal-parent reject, mount-root failures map to `cwd_outside_mount_roots` with a routing hint, and the dead no-eligible-worker rule is gone. The tests now drive the mapping with details copied from the emitting source, so a broker wording change breaks them.
- **The committed `uv.lock` was stale — CI has been running without `pytest-timeout` since it "landed" (audit 2026-07-05).** #16 added `pytest-timeout>=2.2` to the dev extra and set `timeout = 300`, but the lockfile was never regenerated; `uv sync --frozen` (CI) installs the lock verbatim and does **not** validate it against `pyproject.toml`, so the plugin was silently absent and the ini option ignored — no hang protection, the exact gap Tests-1 was meant to close. The lock is regenerated, and two `test_deploy_lint` guards make the failure mode structural: the timeout plugin must be _loaded_ in the running test env, and `uv lock --check` must pass.
- **Live/e2e suites now carry the `live` marker, not just an env `skipif` (audit 2026-07-05, L2).** CI's `-m "not live"` was mostly decorative — only 2 of the live tests were marked; the rest relied on per-module env checks, where one typo'd variable name would run a live suite in CI. `tests/test_e2e.py`, `tests/integration/test_broker_concurrency_live.py`, and `test_project_defaults_live.py` are now structurally deselected (capability-gated tests like `test_cancel_latency`/`test_hook_forward` deliberately keep only their skipif — they legitimately run where the capability exists).
- **`publish-registry.yml` verifies the `mcp-publisher` download against a pinned sha256 (audit 2026-07-05, L4)** — release tags/assets are mutable, unlike the SHA-pinned actions elsewhere — **and the wait-for-PyPI poll is 10 minutes (L5)** so a slow upload (or a future required reviewer on the `pypi` environment) can't fail the registry publish after the tag is already cut. Also: the metrics cache now has a TTL-_expiry_ regression test (hit-within-TTL and TTL=0 were covered; expiry wasn't), and a stale `# v6.0.3` comment on a v7.0.0 action pin is corrected.

### Security

- **`POST /jobs/{id}/log` no longer buffers an oversized body before the size check (audit 2026-07-05, LOW).** `await request.body()` pulled the whole request into memory and only then compared against the 10 MiB cap, so a token-holder could drive broker memory arbitrarily high per request. The body is now rejected from the `Content-Length` header when one is declared, and read in bounded stream chunks with the cap enforced mid-read either way.

## [0.5.10] — 2026-07-03

### Fixed

- **`GET /wait/{id}` streams the log in bounded slices instead of reading the whole backlog into memory (audit 2026-07-01, LOW).** The SSE generator opened the log and did `f.read()` from the last position; on the first iteration `position` is `0`, so attaching `/wait` to a job that had already produced a large log pulled the entire file into one allocation (plus a second full copy on `.decode()`) — for the exact case `/wait` is built for (babysitting a long-running job with lots of output), each connection could balloon broker memory. It now reads in 64 KiB slices (`WAIT_STREAM_CHUNK_BYTES`), reopening the file per slice so no fd is held across an SSE yield, and decodes through an incremental UTF-8 decoder so a multibyte char split across a slice boundary isn't corrupted into replacement chars. A final drain on terminal-state detection also closes a latent race where bytes the worker wrote as it exited (between the read and the state flip) could be dropped.

## [0.5.9] — 2026-07-03

### Fixed

- **Config-mutation endpoints return 422, not 500, on a malformed body (audit 2026-07-01, LOW).** `POST /projects/{name}` and `POST /projects/{name}/nudge` took a raw `dict` and did `int(payload["priority"])` / `int(payload["delta"])`, so a missing key or non-integer value raised `KeyError`/`ValueError` inside the handler → an opaque 500. They now take pydantic request models (`SetPriorityRequest`/`NudgePriorityRequest`), so FastAPI rejects a bad body with a descriptive 422 before the handler runs. The `[0,100]` clamp stays in the handler (out-of-range is clamped, not rejected — unchanged behavior).
- **`job` CLI no longer dumps a Python traceback when the broker is down (audit 2026-07-01, LOW).** Most subcommands wrapped their broker call with no error handling, so a `BrokerUnreachable` (broker down / wrong `JOBD_URL`), `BrokerServerError` (5xx), or `BrokerRefusal` (4xx) surfaced as a raw stack trace; only a few (`preempt`, `preempt-blockers`, `delete-worker`, `ping`) handled any of them. The console entry point is now `job_cli.cli:main`, which wraps the Typer app in a broker-error boundary: a one-line stderr diagnostic + a clean exit code (2 for unreachable/5xx, 1 for a 4xx refusal), never a traceback. It's a backstop — per-command handlers still run first and keep their tailored messages; only exceptions they don't catch reach the boundary, and normal `SystemExit`/`typer.Exit` pass through untouched.

### Security

- **`/metrics` caches its DB aggregation to blunt unauthenticated scrape amplification (audit 2026-07-01, LOW).** `/metrics` is unauthenticated and tailnet-ACL-exempt by design (an in-cluster Prometheus scrapes from the docker-bridge IP, not a tailnet address), so its per-scrape `GROUP BY state` query was an amplification vector — an unauthenticated caller could drive one DB round-trip per request in a tight loop. The collector now caches the aggregate counts for a short TTL (default 5s, `JOBD_METRICS_CACHE_TTL_S`, set `0` to disable), querying under a lock so a burst that all misses the cache still issues a single query (dogpile-proof). Normal Prometheus scraping (15–30s intervals) is unaffected; the ACL exemption is retained since it's load-bearing for scraping.
- **Submitted `env` values are now masked on observability reads (audit 2026-07-01, LOW-Sec).** A job's `env` was stored plaintext and echoed verbatim on every job-info read — `GET /jobs`, `GET /jobs/{id}`, the submit/cancel/preempt responses, and the MCP `jobd_status`/`jobd_job_get` tools (all via `broker.jobinfo._to_info`) — so a token someone passed in `env` leaked to any other token-holder and, worse, into an agent's context on a routine status read. Those surfaces now return `{"KEY": "***"}` (keys preserved so operators can see _which_ vars are set; values hidden). The masking is fail-safe by default in `_to_info`; only the worker-claim `/next-job` path opts out (`redact_env=False`) so the job still runs with the real values. **Not** covered: env is still stored plaintext at rest in `jobs.env_json` (DB-file/backup exposure is out of scope for this change) — see `docs/security.md`. Note: there is no `--env` CLI flag on `job submit` in this tree; env enters only via MCP or a direct `POST /submit`.

### Changed

- **Docker build no longer re-downloads dependencies on every source edit (audit 2026-07-01, Tests-2).** The Dockerfile copied `src` above the single `pip install .`, so any code change busted the layer that resolves and downloads all third-party deps. The install is now two-phase: phase 1 installs only the dependencies (against a stub package, keyed on `pyproject.toml`+`README.md`) and phase 2 copies the real source and reinstalls just the package (`--no-deps --force-reinstall`). A source edit now reruns only the fast phase-2 layer; the dependency layer stays cached. Verified by a double-build (phase-1 `CACHED` after a source change) plus an import/entry-point smoke check of the resulting image. A new `.dockerignore` keeps local artifacts (`__pycache__`, `.venv`, caches, `tests/`, `data/`) out of the build context so they can't bust the `COPY src` layer or bloat the image.
- **mypy now checks the bodies of un-annotated functions (`check_untyped_defs = true`; audit 2026-07-01, Quality-4).** The `mypy src/jobd src/job_cli` gate was passing but largely vacuous over the hot path: mypy skips the bodies of every def lacking a full signature by default, and the broker/worker (`app.py`, `job_worker.py`) are mostly un-annotated endpoint closures and helpers, so their bodies were never type-checked. Enabling `check_untyped_defs` type-checks those bodies (params default to `Any`, so it catches internal inconsistencies, not missing annotations) and produced **zero** new errors — the code was already clean under it. A `tests/test_deploy_lint.py::test_mypy_checks_untyped_defs` guard keeps the flag from being silently dropped.
- **`/submit` and `/resolve` now share one field-resolution cascade (audit 2026-07-01, Quality-3).** Both endpoints hand-encoded the same `CLI > project_default > profile > global` precedence (docs/projects-yaml.md §3) — `/submit` for values, `/resolve` for value+source — and had already drifted: `/resolve`'s profile `host_pin` branch carried a `!= "any"` guard (so a profile `host_hint: any` is not mistaken for a real pin) that `/submit` lacked. The divergence was latent (source-label only; no resolved-value difference today), but any future edit to one copy would silently diverge the other — exactly the class of bug where a dry-run preview stops matching the real submit. The precedence now lives once in `jobd.config.resolve_effective_config`, returning a `(value, source)` per field; `/resolve` surfaces it verbatim and `/submit` reads `.value` (submit-only `vram_gb`/`ram_gb`/`cpus`/`fast_path`, which have no source label and a single consumer, stay inline). A new `tests/test_submit_resolve_agreement.py` locks the invariant: for a matrix of project/profile/CLI inputs, every field `/submit` persists equals what `/resolve` previewed.

## [0.5.8] — 2026-07-02

### Fixed

- **Watchdog kills now escalate to SIGKILL (audit 2026-07-01, H1).** The wall / idle / first-output watchdogs in `job_worker.py` sent a single `SIGTERM` and returned from `poll_signals` without arming a kill timer. A workload that ignores SIGTERM while holding its stdout open (native code in a critical section, or a child holding the pipe) blocked the stdout-read loop forever, so the post-loop `proc.wait(timeout=grace)` escalation was never reached — the job pinned its slot (e.g. a GPU) indefinitely and later cancels went unpolled. All three watchdogs now route through `_initiate_termination`, the same idempotent path the broker cancel/preempt signal uses, which SIGTERMs and schedules the SIGKILL escalation (grace `WATCHDOG_KILL_GRACE_S`, default 60s, now tunable via `JOBD_WORKER_WATCHDOG_KILL_GRACE_S`). Validated end-to-end by a real broker+worker harness (`tests/integration/test_broker_concurrency_live.py`, `JOBD_LIVE=1`) that force-kills an actual `trap '' TERM` workload.
- **`job wait` / `job status --watch` / MCP `wait=true` no longer hang on preempted/orphaned/scheduling_timeout jobs (audit 2026-07-01, Quality-1).** The terminal-state set was defined five times across the broker, CLI, and MCP surfaces, and three copies (`cli.py` single-job `TERMINAL_STATES`, `mcp/tools.py` `_TERMINAL`) were the narrow `{completed, failed, cancelled}` — so any wait loop spun until its timeout when a job reached one of the three newer terminal states. All copies now import a single canonical `TERMINAL_STATES` / `TERMINAL_FAIL_STATES` from `models.py` (frozensets of `JobState`; StrEnum membership works on both raw API status strings and enum values).
- **State transitions are now compare-and-swap guarded (audit 2026-07-01, H3).** Only the matcher's `queued->assigned` claim was atomic; every other broker transition was an ORM read-check-write. The worst case: `cancel_job`'s queued path read `QUEUED`, and if a worker's claim committed before its write, it overwrote `ASSIGNED` back to `CANCELLED` _without_ setting a kill `signal` — the worker then ran the job to completion while the user was told it was cancelled, and the resulting `/complete` was swallowed as a terminal-idempotent no-op. A new `_cas_state` helper (mirroring the claim) now guards `cancel` (both the queued->cancelled write and the running/assigned signal write), `complete` (wins only from a non-terminal state, so a late `/complete` can't clobber a sweeper-set `ORPHANED` and vice versa), `started` (assigned->running), `preempt` (signal write), and — after an independent review found the initial pass had left them unguarded — the sweeper's `scheduling_timeout`, ASSIGNED reclaim, wall-clock and dead-worker orphan writes, plus the heartbeat reconcile. Each is a compare-and-swap on the state it read, so a `/complete` a briefly-revived worker commits between the sweep's SELECT and its trailing commit can no longer be clobbered back to `QUEUED`/`ORPHANED` (which would lose the result and re-dispatch). The worker-liveness check gates the reclaim _decision_; the CAS gates the _write_.
- **Dependency cascade is now transitive and fires on every failed-side terminal (audit 2026-07-01, H2).** Three gaps stranded dependents in `QUEUED` forever. (1) `_cascade_on_parent_terminal` was single-level: in `A<-B<-C`, `A` failing cancelled `B` but left `C` queued — it now traverses transitively (a cancelled child is itself a failed-side terminal, so its own default-policy children cascade). (2) `/submit` only checked that a `depends_on` parent existed; a default-policy dep on an already failed-side-terminal parent (which can never reach `COMPLETED`, and whose cascade already fired) is now rejected with `400` instead of stranding the child. (3) The `scheduling_timeout` sweep and the `cwd_unreachable` refuse-admission path both transitioned the parent to a failed-side terminal without cascading; both now cancel their default-policy dependents (the `scheduling_timeout` deps test previously only passed because it invoked the cascade hook by hand). any-exit dependents are unaffected — any terminal parent satisfies their dep.
- **Requeue paths now clear the pending kill signal (audit 2026-07-01, M1).** A job carrying a `cancel`/`preempt` signal that gets re-queued (both `refuse-admission` branches, the sweeper's ASSIGNED and idempotent-RUNNING reclaims, and the heartbeat reconcile) kept `job.signal` set, so the fresh worker it re-dispatched to read the stale signal on its first `/signal` poll and SIGTERMed the re-run. All requeue sites now reset `signal` to `None` (as the terminal `/complete` path already did).
- **The broker refuses `/log`, `/started`, and `/complete` from a stale worker (audit 2026-07-01, M2).** After a partition, a genuinely-running job could be reclaimed and re-dispatched to a second worker while the first kept running; the first worker's late `/complete` then terminal-ized the job under the wrong worker and its `/log` chunks interleaved into the new run's log. The worker now tags every request with an `X-Jobd-Worker: <hostname>` header (the same value it sends as `host` in `/next-job`, which becomes `job.worker`), and the broker returns `409` when the reporting worker isn't the job's current owner. The same guard covers `/refuse-admission` (added after an independent review found it missing — a stale worker's delayed refusal could otherwise requeue/exclude a job now running on a different worker), whose state writes are also CAS-guarded. Pre-header workers (no header) are unaffected — the check is skipped, preserving the old best-effort behavior.
- **`cwd_refused` is emitted after commit (audit 2026-07-01, M3).** The `cwd_missing` refuse-admission path emitted its `cwd_refused` event before the transaction committed — the lone violator of the emit-after-commit invariant, so a failed commit could leave `events.jsonl` claiming a refusal the DB never recorded. It now emits after the commit in both the re-route and `cwd_unreachable` branches.
- **Worker VRAM ads no longer double-count a running job (audit 2026-07-01, M6).** The heartbeat subtracted the full sum of in-flight VRAM reservations from the live NVML free reading — but NVML free already reflects the VRAM those jobs have allocated, so a steady multi-slot worker understated its free VRAM by Σ(ads) and idled capacity it actually had. The worker now reserves only the _unallocated_ part of each ad, `max(0, ad − already_allocated)` (computed from per-pid NVML usage of the worker's own job pids, the mirror of the foreign-VRAM accounting): a job still in its CUDA-init startup window gets the full reservation (so a second big job can't overcommit), while a job that has allocated its VRAM reserves ~0. The live `/next-job` admission gate remains the safety net.
- **`/next-job` long-poll no longer parks a threadpool thread, and the sweep no longer freezes the event loop (audit 2026-07-01, M4/M5).** `/next-job` was a sync endpoint that held one anyio threadpool token (default ~40) for its entire long-poll wait; a fleet of ~40 long-pollers starved `/heartbeat` and the other sync endpoints, so workers were spuriously marked stale/orphaned — self-amplifying. It is now `async`: the blocking claim runs via `asyncio.to_thread` (asyncio's executor, separate from the anyio pool) and the wait suspends on an `asyncio.Event` on the loop, so a parked long-poll holds no threadpool token. Wake-on-submit is preserved via a cross-thread `call_soon_threadsafe` with a swap-on-wake event (a wake that fires during a claim attempt can't be lost). Separately, `_sweep_once` now runs via `asyncio.to_thread` from the sweep loop instead of directly on the event loop, so a sweep (incl. its up-to-5s SQLite `busy_timeout` stalls) no longer freezes every async endpoint.

### Changed

- **CI: added `pytest-timeout` with a 300s per-test cap.** A hung subprocess/socket/watchdog test previously stalled CI to GitHub's 6h job kill; each test now fails after 5 minutes instead. Override per test with `@pytest.mark.timeout(N)`.

## [0.5.7] — 2026-07-01

### Added

- **Unauthenticated Prometheus `/metrics` endpoint.** The broker exposes aggregate state for scraping — `jobd_jobs{state}`, `jobd_workers{state}`, and `jobd_build_info{version}` — computed from the DB on each scrape via a `prometheus_client` custom collector (private registry; only `jobd_*` series, no default process collectors). It is mounted as an ASGI sub-app so it bypasses the global bearer-token dependency (mounts don't inherit router deps), and its `/metrics` path is exempted from the tailnet-IP ACL so an in-cluster Prometheus — whose source IP is the docker bridge, not a tailnet address — can scrape the broker's tailnet-bound port. Only non-sensitive aggregate counts are exposed, and the endpoint stays reachable only on the broker's bound interface. New dependency: `prometheus-client`.

## [0.5.6] — 2026-06-30

### Fixed

- **Host-local cwds no longer silently misroute to `exit 127`.** A job whose `cwd` exists only on some hosts (e.g. a git worktree under `/home`, which **every** worker advertises in `mount_roots`) used to pass the broker's coarse `/next-job` prefix filter, route to a host that lacked the path, and die `[worker setup error] No such file or directory` / `exit 127` minutes later. Two layers now prevent this. **(A) Submit-time probe:** `cwd_routability` (`matcher.py`) checks the cwd against known workers' `mount_roots` — a pinned host that advertises no covering root — or a `host_pin=any` cwd that **no** known worker covers — gets a `400` with a routable hint (generalizing the prior `/mnt/c` guard), since the job can't run anywhere. Workers with empty `mount_roots` are treated as "unknown" so it never false-rejects. **(B) Worker-side re-queue:** before running, a worker verifies `os.path.isdir(cwd)`; if absent it POSTs `refuse-admission` `reason=cwd_missing` instead of `exit 127`. The broker records the host in a new per-job `jobs.excluded_workers_json` (auto-migrated), drops the job for that host at `/next-job`, and re-routes — or fails it `termination_reason="cwd_unreachable"` with an explanatory log line when no eligible worker remains (never a silent queue, never a hot loop: each worker refuses a given job at most once). `AdmissionRefusal` gains `reason`/`cwd` and relaxes `required_gb`/`free_gb` to optional; the GPU-contention admission path is unchanged. Old workers (no cwd check, empty `mount_roots`) keep working. Motivating incident: a jepagame GPU test submitted from a worktree cwd with `--gpu` (2026-06-30). Design + plan: `docs/plans/2026-06-30-submit-cwd-probe-design.md`, `…-submit-cwd-probe.md`; deploy: `…-submit-cwd-probe-deploy.md`.

## [0.5.5] — 2026-06-11

### Added

- **`/gpu-holders` now attributes GPU-holding PIDs to jobd jobs (#7).** Workers report a per-job PID inventory in every heartbeat (`in_flight_pids`: the job's top-level pid plus its live scope-cgroup pids — the same ownership boundary the foreign-VRAM accounting uses). The broker stores the latest map per worker (`workers.in_flight_pids_json`, auto-migrated) and `/gpu-holders` resolves each probed pid against it: `known` becomes meaningful and rows gain `job_id` + `worker` attribution. PIDs are host-local, so `?host=<worker>` scopes the lookup to one worker's inventory; the default consults every online worker (documented pid-collision caveat on multi-host fleets). Old workers that don't report the field behave exactly as before.

### Fixed

- **Worker SIGTERM now drains in-flight jobs instead of stranding them.** The old handler called `sys.exit(0)` from the signal handler: daemon job threads died without signaling their workloads or posting `/complete`, the orphaned subprocess kept running, and — because `Restart=on-failure` brought the worker back within seconds, refreshing the heartbeat the dead-worker reaper keys on — the job could stay `RUNNING` in the broker forever. The handler also posted an httpx event from inside the signal context, which can deadlock on the client's pool lock. Now the handler only sets the stop flag; after the poll loop exits, the worker preempts every in-flight job through its normal cancel/preempt machinery (checkpoint window honored, SIGKILL escalation after `min(checkpoint_grace_s, JOBD_WORKER_DRAIN_GRACE_S)`, default 60s), joins job threads against a bounded deadline, and posts a `worker_shutdown` event with a `signaled`/`aborted` summary. Drained jobs land as `preempted` with `termination_reason="worker_shutdown"`. Jobs claimed mid-drain are refused before Popen and completed as preempted. Single-slot workers now run jobs in a thread like multi-slot ones (the inline path parked the main thread for the whole job, making a drain impossible). `job-worker.service` gains `KillMode=mixed` + `TimeoutStopSec=150`. Design: docs/plans/sigterm-drain.md.
- **Broker reconcile backstop for workers that die without draining (Phase 2).** A SIGKILLed/crashed/power-lost worker restarts within seconds under `Restart=on-failure` and heartbeats again — refreshing the liveness clock the dead-worker reaper keys on — while knowing nothing about the jobs it was running, stranding them in `RUNNING`/`ASSIGNED` forever. Workers now report their in-flight job ids in every heartbeat (`in_flight_job_ids`; old workers that omit the field are skipped entirely), and the broker gives any claim older than 60s that is absent from 2 consecutive reports the worker-died disposition: `ASSIGNED` jobs requeue (also recovering a lost `/next-job` response, which the heartbeat-keyed reaper never caught), idempotent `RUNNING` jobs requeue, non-idempotent `RUNNING` jobs go `ORPHANED` with `termination_reason="worker_restarted"` and cascade to dependents. New `jobs.reconcile_misses` column (auto-migrated). Both deploy orders are safe — the heartbeat field is additive.
- **Startup sweep of stale `jobd-*.scope` units (Phase 3).** Scope-wrapped workloads survive an undrained worker death (scopes live outside the worker service's cgroup), so a job requeued by the Phase 2 reconcile could re-dispatch and double-execute against the still-running old workload. The worker now kills every leftover `jobd-<id>.scope` at startup — before its first poll — via cgroup-walk (or `systemctl --user kill` when the cgroup path can't be resolved) and emits a `stale_scope_sweep` event listing the units. Assumes one jobd worker per user session, which is the deployment model.
- **2026-06-10 audit batch.** Children of a `scheduling_timeout` parent no longer strand in QUEUED forever (the state was missing from the dependency-terminal and cascade-trigger sets); `POST /jobs/{id}/log` now 404s on unknown jobs and 413s chunks over 10 MiB; cascade-cancel events are emitted after the transaction commits instead of inside it; the worker retries the `/complete` POST with backoff so a transient broker blip can't lose a job result; SIGKILL-escalation timers are cancelled on process exit; the post-SIGKILL `proc.wait()` is bounded (30s, rc −9); `tracked_pids` reads/writes are lock-guarded; the broker strips `JOBD_API_TOKEN` like clients already did; and `JOBD_ALLOW_NO_AUTH=1` + `JOBD_DISABLE_TAILNET_ACL=1` on a non-loopback bind now refuses to start instead of warning.

### Changed

- **mypy is now a blocking CI gate.** Added a `[tool.mypy]` config and dropped `continue-on-error` from the type-check step, so type regressions fail the build. Cleared the pre-existing type debt with real fixes (a `BrokerState` TypedDict, `Literal` resolution-source annotations, `None`-narrowing) and targeted `# type: ignore[attr-defined]` only for genuine SQLAlchemy/mypy false-positives. No runtime change.

## [0.5.4] — 2026-06-07

### Fixed

- **Broker-side wall-clock backstop in the RUNNING reaper.** A job whose worker crashed mid-run and then restarted _within_ `DEAD_WORKER_SECONDS` (300s) could be stranded in `RUNNING` forever: `max_wall_s` was only enforced worker-side (`job_worker.poll_signals` SIGTERMs at `max_wall_s`), and that per-job monitor dies with the worker. The restarted worker has no memory of the job, so it never kills it and never posts `/complete`; the broker's reaper saw a live heartbeat again and left the job running. The reaper now also orphans any RUNNING job that has blown past `max_wall_s + checkpoint_grace_s + 120s` regardless of worker liveness (`termination_reason="wall_clock_exceeded"`), cascading to dependents like the existing `worker_died` path. The 120s grace keeps the broker strictly looser than a healthy worker (which terminates + reports within seconds of `max_wall_s`), so it never races one. Jobs submitted without `max_wall_s` are unaffected by design.

## [0.5.3] — 2026-06-07

### Added

- **Listed on the official MCP registry.** Added `server.json` plus a `publish-registry.yml` workflow that submits it via GitHub Actions OIDC (`mcp-publisher login github-oidc`) on every published release — no stored credentials, mirroring the existing PyPI trusted-publishing. An `mcp-name: io.github.musharna/jobd` marker in the README lets the registry verify the PyPI package belongs to this server. Because the MCP server is the `jobd-mcp` entry point behind the `[mcp]` extra (not the default `jobd` broker entry point), the registry entry runs it as `uvx --from jobd[mcp] jobd-mcp`. Auto-propagates the listing to downstream directories (PulseMCP, etc.). No code or runtime change.

## [0.5.2] — 2026-06-03

### Fixed

- **Worker no longer warns + buffers job output.** The job subprocess was opened with `bufsize=1` (line buffering) in binary mode (`text=False`), which Python doesn't support — it emitted a `RuntimeWarning` on every job and fell back to a `BufferedReader` whose `read(4096)` blocks until 4096 bytes accumulate, delaying streamed logs. Now `bufsize=0` (unbuffered): the warning is gone and each chunk read returns on first available data, so logs stream promptly.
- **Docker image now builds.** The Dockerfile only `COPY`ed `pyproject.toml` + `src`, but `pyproject.toml` declares `readme = "README.md"`, which hatchling reads during metadata generation at `pip install .` → build failed with `OSError: Readme file does not exist`. Added `README.md` to the `COPY`. (The public Docker build path isn't exercised by CI, so this was latent.)

### Documentation

- **Non-root image + host-owned bind-mount gotcha.** The image runs as uid 10001; a bind-mounted `data/`/`logs/` owned by a different host uid makes the broker crash-loop on `attempt to write a readonly database`. Documented the two fixes (chown to 10001, or run the container as your host user via `user:`) in `docker-compose.yml` and the runbook, plus a runbook note on repointing a stale worker-unit `ExecStart` at the `jobd-worker` console script.

## [0.5.1] — 2026-06-02

### Documentation

- **README "Retention" section** documenting `JOBD_JOB_RETENTION_DAYS` — the opt-in knob added in 0.5.0 was only described in the runbook, not the main README's Configuration area (the 0.3.0 multislotting knob set the precedent of documenting user-facing config in the README). No behavior change.

## [0.5.0] — 2026-06-02

### Added — job/log retention

- **`JOBD_JOB_RETENTION_DAYS` prunes old terminal jobs.** When set to a positive number, the sweeper deletes jobs in a terminal state (`completed`/`failed`/`cancelled`/`preempted`/`orphaned`/`scheduling_timeout`) whose `finished_at` is older than that many days, along with their per-job `.log` files, and emits a `jobs_pruned` event with the count. This bounds jobs-table and log-dir growth on a long-running broker (the `events.jsonl` stream is already bounded by size-rotation). Opt-in: the default (`0`) keeps history forever, so existing deployments are unchanged. Freed SQLite pages are reused under WAL, so the DB file stays bounded without a global-locking `VACUUM`. Pruning old terminal parents is safe for any still-pending dependents — they're already treated as dependency-satisfied.

## [0.4.0] — 2026-06-02

### Changed — server-side long-poll for `/next-job`

- **The broker now holds `/next-job` until a job is dispatchable.** Previously the endpoint returned `null` instantly and the worker re-polled every 2s, so each idle worker drove ~15× the intended request rate (a full queued-scan + per-dep lookups every 2s) even with nothing to do — the worker's own 30s client timeout showed a long-poll was the original design intent, never built. The endpoint now blocks up to `wait_s` (the worker sends its `POLL_TIMEOUT_S`, 30s), re-attempting whenever a `/submit`, terminal transition, or requeue wakes it (a `threading.Condition`), and otherwise rechecking at most every 10s as a backstop. Result: an idle worker makes ~0 requests while waiting, and a freshly submitted job dispatches near-instantly instead of after up to 2s.
- **Backward-compatible.** `wait_s` defaults to `0` (legacy instant return), so older workers — and any client that omits it — are unaffected. A worker talking to an older broker that ignores `wait_s` gets an immediate `null`; the worker detects the fast return (elapsed < half its timeout) and keeps the 2s backoff, so it never hot-loops.

### Added — events.jsonl rotation + bounded reverse-read

- **Size-based rotation.** The broker's `events.jsonl` is rotated to `events.jsonl.1` (one backup, overwriting any prior) once it crosses `JOBD_EVENTS_MAX_BYTES` (default 50 MB), bounding on-disk retention to ~2× the threshold. `job audit` / `GET /events` read both files, so a query spanning a rotation returns continuous history within the window. New `jobd.events` module is the single write choke-point (serialized by a lock so a concurrent rotate+append can't race).
- **Bounded reverse-read.** `GET /events` now reads newest→oldest and, with a `--since` cutoff, stops at the first row older than it instead of scanning the whole file — events are append-ordered by the broker's server-side timestamp, so the early-stop is exact. Output order, filters, and `limit` semantics are unchanged.

### Fixed

- **Removed a stale runbook "Known issue"** describing the self-GPU-as-foreign-VRAM reporting bug, which was fixed in 0.3.0.

## [0.3.0] — 2026-06-01

### Fixed — Worker's own multi-process GPU job mis-counted as foreign VRAM

- **GPU foreign-VRAM accounting now uses the scope cgroup, not a single pid.** A GPU job's CUDA contexts can live in forked children (DDP / accelerate / dataloader workers, or a `bash -c` that spawns the trainer) under pids different from the one the worker tracked. NVML reports whichever pid holds the VRAM, so the worker counted its _own_ children as foreign — inflating heartbeat `unregistered_vram_gb` (spurious broker contention) and, at `JOBD_WORKER_MAX_CONCURRENT_JOBS>1`, refusing the worker's own second job. `compute_unregistered_vram` and the live admission gate now exclude `_effective_owned_pids()` = the tracked pid unioned with every live pid in each in-flight job's `jobd-<id>.scope` cgroup (the same ownership boundary the cancel/reap paths already use). Verified on an RTX 5090: a parent that forks a CUDA-holding child shows NVML reporting the child pid, which is absent from the tracked set but present in `cgroup.procs`.

### Added — Multislotting observability

- **Slot usage in `job workers`.** Each worker's heartbeat now carries `max_concurrent` (its `JOBD_WORKER_MAX_CONCURRENT_JOBS`) and `running` (live in-flight job count), persisted on the worker row (additive migration) and surfaced on `WorkerInfo` / `job workers` as `running`/`max_concurrent`. Pre-field workers read as `1`/`0`. Lets you see at a glance how full each worker's slots are when bin-packing CPU + GPU jobs.
- **README "Concurrency (multislotting)" section** documenting `JOBD_WORKER_MAX_CONCURRENT_JOBS`: the matcher is resource-aware, so raising the limit bin-packs jobs that fit side by side (a CPU-only job co-runs with a GPU job; two GPU jobs co-run only if both fit live VRAM) rather than blind oversubscription.

### Added — Parameter sweeps

- **`job submit --sweep KEY=v1,v2,v3`** (repeatable) fans out a job array over the cartesian product of named axes — a grid search in one call. Each member substitutes `{KEY}` → its value in the command and env; `{i}` (the flat 0-based member index) is available alongside the named keys. `--sweep lr=0.1,0.01 --sweep seed=1,2,3` yields 6 members. `--sweep` and `--count` are mutually exclusive, the product is capped at 1000, and `i` is reserved as an axis key. Reuses the array machinery (shared `array_id`, `job list --array`, `job status A<id>`) and the literal-`{key}`-replace substitution from `--count`. The `jobd_submit` MCP tool accepts `sweep` (list of `{key, values}`); the broker `JobSubmit` model gains a `sweep: list[SweepAxis]` field.

## [0.2.0] — 2026-06-01

### Added — Job arrays

- **`job submit --count N`** fans out one command template into N array members in a single call. Each member is an ordinary job (independent routing, preemption, checkpointing); `{i}` in the command — and in env values via the API/MCP — is replaced by the member's 0-based index. The substitution is a literal `{key}` replace (not `str.format`), so commands containing JSON literals or shell braces pass through untouched. The engine is generic over named keys, leaving room for a future `--sweep` form.
- **Array identity + inspection.** Members share an `array_id` (the first member's job id), plus `array_index` / `array_size`, surfaced on `JobInfo`. New `job list --array A<id>` filters to one array; `job status A<id>` prints an aggregate state tally + per-member rollup and exits non-zero if any member ended non-completed. The broker `/submit` returns `{array_id, count, job_ids, warnings}` for `count>1` (a single `JobInfo` for `count==1`, unchanged); `GET /jobs` gains an `array_id` filter. The `jobd_submit` MCP tool accepts `count` and returns the array summary.

### Changed — Worker is now a packaged, installable component

- **`jobd-worker` console script.** The worker daemon and its capability detection moved into the `jobd` package (`jobd.worker.job_worker`, `jobd.worker.capabilities`) and now install as a `jobd-worker` entry point via `pip install "jobd[worker]"`. No clone, no manual file copy, no `python worker/job_worker.py`. The `[worker]` extra carries the runtime deps (httpx, psutil, pyyaml, nvidia-ml-py).
- **Simplified worker setup.** `scripts/install-worker.sh` now `pip install`s `jobd[worker]` into `~/jobd-worker/.venv` instead of copying source files from a checkout. The two systemd templates collapsed into one `scripts/job-worker.service` (`ExecStart=…/jobd-worker`) — the repo-checkout vs standalone-copy distinction is gone now that imports resolve from the installed package.

## [0.1.0] — 2026-05-31

Initial public release. The bullets below summarize the capabilities built during
pre-release development; parenthetical short hashes reference that internal history
and are not present in this repository's squashed tree.

### Added — Auto-preempt protocol

- **#8 path A — auto-preempt on queue-age** (`6db2712`): broker sweeper auto-preempts running jobs when their host has a higher-priority candidate waiting and queue age crosses threshold.
- **#28/#29 manual preempt-blockers + warnings-only filter** (`b06e78d`): `--no-preempt` submit flag and warnings-only `job list --warnings` filter.
- **#32 `events.jsonl` auto-preempt event** (`e719842`): broker emits a structured event each time a sweep fires, for observability.
- **#34 `/preempt` accepts ASSIGNED-but-not-started state** (`22a3791`): pinned by test.
- **#35 `depends_on` cascade on PREEMPTED parent** (`26e56ac`): pinned by test.
- **#8 path B — checkpoint protocol** (`e7593b5`): SIGTERM grace window with `JOBD_CHECKPOINT_GRACE_S` knob; terminal `preempted` state with `RESUME_FROM` operator-driven resume contract.
- **`JOBD_CHECKPOINT_DIR` env var** (`f0cb754`): exposed to every job, pointing at a per-job directory `${JOBD_WORKER_CHECKPOINT_ROOT:-$XDG_DATA_HOME/jobd/checkpoints}/<job_id>/` (default `~/.local/share/jobd/checkpoints/<job_id>/`). Workloads write durable preempt-time checkpoints here; broker does not sweep contents.
- **`JOBD_WORKER_CHECKPOINT_ROOT` operator override** (`71ae23b`, fix-forward `bf195bc`): worker-level env var redirects checkpoint root (e.g., to a faster filesystem); `os.path.expanduser` applied.

### Added — Resource-aware admission & GPU matching

- **#42 heartbeat-aware GPU matcher** (`a3509c9`): matcher consults live worker heartbeats (`unregistered_vram_gb`) and drops saturated hosts; 2 GB VRAM floor on `--gpu`.
- **#42 multi-tenant per-host co-routing** (`f9e6152`): `JOBD_WORKER_MAX_CONCURRENT_JOBS` enables threaded per-host dispatch with per-job thread tracking.
- **`unregistered_vram_gb` in `/workers`** (`93795b0`): exposed for matcher + observability.
- **#41 resource-aware admission at dispatch** (`53ffc09`): NVML probe → admission gate → `/jobs/{id}/refuse-admission` re-queue path; tier-tag inference; bypass marker.
- **#41d `vram_gb` on `JobSubmit`** (`ed154f5`): CLI `--vram-required` flag now persists end-to-end.
- **#43 unsatisfiable-placement preflight** (`bf15ffa`): submit-time check rejects jobs whose `needs:` tags or VRAM ask cannot be satisfied by any registered worker.
- **CUDA VRAM tier tags** (`860fdf6`): `cuda-32gb`, `cuda-12gb`, etc. so `needs:cuda-32gb` routes only to matching hosts.
- **`cuda-8gb` tier** (`a8ed96d`): added so RTX 2080-class GPUs advertise a discoverable tier.

### Added — Scheduling, watchdogs, MCP

- **Per-job idle-output watchdog + max-wall timeout** (`5f074c0`): broker-side wall enforcement; idle-output watchdog terminates silent-hung jobs.
- **`--max-wall` and `--idle-timeout` flags on `job submit`** (`f105724`).
- **Scheduler-awareness warnings** (`0ef4497`): single-slot stall + queue-age-blocked-by-load surface as `warnings:` on `/jobs/{id}`.
- **Per-job time estimation v1** (`1642d5d`): `eta_*` fields on `Job` and `--eta` flag on `job list`.
- **Default-on ETA banner on `job submit`**: `--eta/--no-eta` defaults on; prints `Estimated wall p50 X, p90 Y (n=N prior runs)` or `ColdStart` line to stderr after submit. Closes BACKLOG "Auto-surface ETA on submit" Part 1.
- **ETA Part 2 — ctest-aware sub-job parsing**: opt-in via `JOBD_CTEST_PARSE=1`. New module `src/jobd/ctest_eta.py` parses `<cwd>/build*/Testing/Temporary/CTestCostData.txt`, filters tests by the `ctest -R <regex>` arg, and sums avg-cost values. Broker reports `eta_basis="ctest-cost-K=<n>"` ahead of history-based prediction; CLI banner renders `Estimated wall ~Xs (ctest cost-data, k=K tests)`. Falls through to history when env unset, regex misses, or cost file absent. Closes BACKLOG "Auto-surface ETA on submit" Part 2.
- **First-byte smoke watchdog — pieces 2+3**: worker-side, in `worker/job_worker.py`. Piece 3 (pre-dispatch launcher-existence check) verifies `cmd[0]` exists and is executable when it looks like a path (`/`, `./`, `../`); on miss, POSTs `/complete` with `termination_reason="launcher_missing"` instead of exec-ing into a silent exit-127. Piece 2 (first-output watchdog) adds env var `JOBD_WORKER_FIRST_OUTPUT_TIMEOUT_S`: fires once if no stdout byte lands within N seconds of job start, disarms permanently on first byte. Both surface as `final_state="failed"` for depends_on cascades. Closes BACKLOG "First-byte smoke" pieces 2+3; piece 1 (push-on-terminal-failure) deferred.
- **Per-project defaults block in `projects.yaml`** (`34c2a30`): keys at submit time inherit from project block when omitted.
- **`fast_path` field honored in `JobSubmit`** (`557a0e8`).
- **#51 `submitted_via` marker** (`636969c`): `JobSubmit.submitted_via: Literal["cli", "mcp"]` round-trips through translation layer; structural test pinned.

### Added — Worker management & deploy

- **DELETE `/workers/{host}` for purging stale registrations** (`ea04d6a`).
- **`job delete-worker` + `jobd_worker_delete` MCP tool** (`ccdf35c`): exposes the DELETE endpoint via CLI and MCP.
- **`jobd-broker.service` systemd unit** (`5334a5c`, #52): with tailscale IP wait.
- **`job-worker.laptop.service` variant** (`c4591b0`): for full-repo-checkout hosts (vs. the server standalone install).
- **Nightly live integration test cron wrapper** (`ef24309`): runs `tests/mcp/test_live.py` against the live broker.
- **Audit instrumentation pass** (`07aa2cf`): coverage inventory + gap report against the existing observability surface.

### Fixed

- **`jobd --help` / `--version` no longer crash with SQLite OperationalError**: entry point now parses argv before `build_app()`, so `--help`/`--version` short-circuit cleanly without touching the database. Help text documents `JOBD_CONFIG_DIR` / `JOBD_DB_URL` / `JOBD_PORT` / `JOBD_LOGS_DIR`.
- **`#51` install-worker no longer writes static `tags:` to `worker.yaml`** (`5c01882`): tags now come from runtime probe.
- **`#73eaa46` cancel via `systemctl kill` on named scope** rather than `Popen.pid`: ensures the entire scope tree dies, not just the tracked PID.
- **`59561aa` ASSIGNED → RUNNING transition** so cancel reports SIGTERM correctly.
- **`969abc3` MCP `log_tail` and `depends_on` field-name alignment** (mcp-v1 field-test fallout).
- **`353a4fb`** clear pyright noise in `_print_resolved`/`_row`.

### Documentation

- **Checkpoint directory contract** in `docs/preemption.md` (`8b5e339`, fix-forward `7ebffca`, final-review polish `2eec63b`): canonical surface for workload authors; covers env vars, default root, mode-0700 + cross-user-resume caveat.
- **Auto-preempt default-flip design spec** (`37e2dc4`) + amendment (`31e843d`).
- **Auto-preempt jobd-side implementation plan** (`915970f`).
- **Backlog reconciliation** (`ddca65e`, `d34f3b0`, `6d0fbaf`, `b257cca`, `354e043`, `ab8f7f7`, `7eb1203`, `394269c`, `b8de199`): Open vs Done sweep after the 2026-04-27..04-29 ship train; field-test backlog items filed.
- **Projects.yaml defaults — implementation blueprint** (`c98cf26`).
- **CHANGELOG.md created** (`86b4880`); this entry backfills the gap from `mcp-v1` to current tip.

---

## [mcp-v1] — 2026-04-26

Translation-layer/MCP shipping point. Earlier history is not catalogued here; see `git log mcp-v1` for detail.
