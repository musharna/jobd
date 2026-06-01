# Changelog

All notable changes to jobd. Format roughly follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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
