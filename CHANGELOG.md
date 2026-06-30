# Changelog

All notable changes to jobd. Format roughly follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- **Host-local cwds no longer silently misroute to `exit 127`.** A job whose `cwd` exists only on some hosts (e.g. a git worktree under `/home`, which **every** worker advertises in `mount_roots`) used to pass the broker's coarse `/next-job` prefix filter, route to a host that lacked the path, and die `[worker setup error] No such file or directory` / `exit 127` minutes later. Two layers now prevent this. **(A) Submit-time probe:** `cwd_routability` (`matcher.py`) checks the cwd against known workers' `mount_roots` — a pinned host that advertises no covering root gets a `400` with a routable hint (generalizing the prior `/mnt/c` guard), and a `host_pin=any` cwd no worker covers gets a submit warning. Workers with empty `mount_roots` are treated as "unknown" so it never false-rejects. **(B) Worker-side re-queue:** before running, a worker verifies `os.path.isdir(cwd)`; if absent it POSTs `refuse-admission` `reason=cwd_missing` instead of `exit 127`. The broker records the host in a new per-job `jobs.excluded_workers_json` (auto-migrated), drops the job for that host at `/next-job`, and re-routes — or fails it `termination_reason="cwd_unreachable"` with an explanatory log line when no eligible worker remains (never a silent queue, never a hot loop: each worker refuses a given job at most once). `AdmissionRefusal` gains `reason`/`cwd` and relaxes `required_gb`/`free_gb` to optional; the GPU-contention admission path is unchanged. Old workers (no cwd check, empty `mount_roots`) keep working. Motivating incident: a jepagame GPU test submitted from a worktree cwd with `--gpu` (2026-06-30). Design + plan: `docs/plans/2026-06-30-submit-cwd-probe-design.md`, `…-submit-cwd-probe.md`; deploy: `…-submit-cwd-probe-deploy.md`.

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
