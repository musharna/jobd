# jobd — Cross-Session Job Queue — Design Spec

> Date: 2026-04-18
> Target repo: new `jobd/` on gt76 (to be initialized after spec approval)
> Target deployment: Docker compose service in `~/homelab/` GitOps stack

## Context

The user runs multiple concurrent research projects — Orchid SDXL, Phelipanche RNA-seq, Synthetic Souls (jepagame), Evo2, ARFD, AutoSilhouette — all sharing three compute hosts: an MSI laptop (RTX 4080, 24GB WSL RAM), a GPU desktop (RTX 5090, 32GB VRAM), and the gt76 homelab server (62GB RAM, weak GPU). Multiple Claude Code sessions can run simultaneously across project directories.

Current state has no coordination between sessions:

- `heavy-run` caps memory per-host via cgroups but is blind to other hosts and other sessions.
- A Ray cluster exists (head on gt76, workers on desktop-WSL + laptop) but is Python-centric and not used for general orchestration.
- Collisions today are prevented by the user manually remembering "don't start X while Y is running."
- Heavy training jobs (SDXL LoRA, RNA-seq pipelines, C++/CUDA sims) can conflict on GPU VRAM or OOM the laptop.
- The user's own audit (`audit_sessions_2026-04-14`) flagged "heavy-script rules unenforced" as chronic — documentation-only enforcement drifts.

This spec defines **jobd**: a lightweight, durable, priority-aware job queue that multiple Claude Code sessions submit work to, with layered enforcement so the queue is actually used.

## Goals

1. **Durable queue** — jobs survive Claude session death, broker restart, worker crash.
2. **Priority-aware scheduling** — project defaults + per-job override, FIFO within ties, newer submissions yield.
3. **Resource-aware matching** — fine-grained VRAM/RAM/CPU requirements via named profiles; hybrid pin (job may pin to host, or declare `any`).
4. **Soft preemption** — jobs marked `preemptible: true` receive SIGTERM + grace window when higher-priority work arrives; non-preemptible jobs run to completion.
5. **Non-queue awareness** — GPU probe via `nvidia-smi` catches manually launched work and routes other jobs around it automatically.
6. **Fast-path for trivial jobs** — small/quick commands skip the queue dance and run locally with minimal overhead.
7. **Claude-friendly** — Claude submits, gets an ID, monitors actively; `job` CLI + HTTP API, not an SDK.
8. **Enforcement** — Claude sessions actually route heavy work through jobd, not bypass it.
9. **Heterogeneous workloads** — bash, Python, R, C++, SSH — any shell command works.
10. **Homelab-pattern fit** — deploys via the existing GitOps compose stack; observable via existing Prometheus + Grafana + Alertmanager + ntfy.

## Non-goals (v1)

- Web UI (Grafana dashboard + CLI are sufficient).
- Job DAGs / dependencies / workflow orchestration (use Snakemake or Ray for that; jobd is a queue).
- Multi-user ACLs / authentication (single-user system).
- Container/image-based job execution (jobs run on host with existing venvs).
- Multi-tenant billing / hard quotas.
- Cross-host filesystem sync (existing DVC + NAS patterns already handle data).
- HA for the broker (gt76 down = homelab down; HA not worth the complexity).

## Architecture

### Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  gt76 (homelab server, Docker compose stack)                    │
│  ┌──────────────────────┐    ┌──────────────────────────────┐   │
│  │ jobd (FastAPI+SQLite)│←──→│ Grafana dashboard (existing) │   │
│  │  • queue + priority  │    └──────────────────────────────┘   │
│  │  • profiles registry │    ┌──────────────────────────────┐   │
│  │  • GPU probe cache   │    │ Prometheus exporter on :PORT │   │
│  │  • classifier rules  │    │  (queue depth, per-host GPU) │   │
│  │  • HTTP API + CLI    │    └──────────────────────────────┘   │
│  └──────────┬───────────┘                                       │
│             │ (also runs a local worker)                         │
└─────────────┼───────────────────────────────────────────────────┘
              │ Tailscale HTTP
      ┌───────┴──────────────┬────────────────────────┐
      ▼                      ▼                        ▼
┌──────────────┐     ┌──────────────┐        ┌──────────────┐
│ desktop-wsl  │     │ msi laptop   │        │ gt76 worker  │
│ job-worker   │     │ job-worker   │        │ (same host)  │
│ (systemd)    │     │ (systemd)    │        │ (systemd)    │
│ • polls jobd │     │ • polls jobd │        │ • polls jobd │
│ • nvidia-smi │     │ • nvidia-smi │        │ • nvidia-smi │
│ • runs cmds  │     │ • runs cmds  │        │ • runs cmds  │
│   under      │     │   under      │        │   under      │
│   heavy-run  │     │   heavy-run  │        │   heavy-run  │
└──────────────┘     └──────────────┘        └──────────────┘
      ▲                     ▲                         ▲
      │                     │                         │
  Claude session        Claude session            Claude session
  (via `job`            (via `job`                (via `job`
   CLI + hook)           CLI + hook)               CLI + hook)
```

### Placement decisions

- **jobd runs on gt76** as a Docker compose service alongside the existing monitoring stack. Single-node, no HA. SQLite for durability.
- **One job-worker per host** (systemd user unit). Workers are stateless; all state lives in jobd. Crash + restart is safe.
- **Communication is HTTP over Tailscale.** Debuggable with curl, no SDK lock-in. No gRPC, no Ray RPC, no Celery.
- **`job` CLI is a thin HTTP client**, installed globally on every host. This is what Claude sessions invoke.
- **jobd itself runs a local worker on gt76** so gt76's CPU/RAM/GPU is available to the cluster.

## Components

### jobd (broker)

Single Python process on gt76. FastAPI + SQLite (upgradable to Postgres via SQLAlchemy if needed).

**Responsibilities:**

1. **Queue management** — SQLite tables: `jobs`, `workers`, `profiles`, `projects`, `classifier_rules`, `bypass_log`, `events`.
   Job states: `queued → assigned → running → (completed | failed | cancelled | preempted | orphaned)`.
2. **Matcher** — when a worker long-polls "got anything for me?", jobd picks the highest-priority job whose required resources fit and whose host pin is satisfied.
3. **Priority engine** — `effective_priority = project_priority + job_priority_override`; ties break by `submit_time ASC` (FIFO, newer yields).
4. **Profile registry** — named resource bundles, YAML-loaded, SIGHUP-reloaded.
5. **Classifier rules** — YAML-loaded, SIGHUP-reloaded, served via `/classify` to the hook.
6. **GPU probe cache** — workers POST nvidia-smi readings every 5s; matcher subtracts unregistered VRAM from the available pool.
7. **Log relay** — workers stream stdout/stderr via chunked POST to `/jobs/<id>/log`; jobd writes `logs/<id>.log` and serves via SSE.
8. **Preemption** — high-priority arrival triggers SIGTERM + grace window on a preemptible holder; worker exits with checkpoint; jobd requeues with original submit_time preserved.
9. **HTTP API** — `/submit`, `/jobs`, `/jobs/<id>`, `/wait/<id>` (SSE), `/cancel/<id>`, `/workers`, `/classify`, `/profiles`, `/projects`, `/bypass-log`, `/reload`, `/metrics`.

### job-worker

Systemd user unit on each host (gt76, desktop-WSL, msi laptop). Single-file Python script (`httpx` + `psutil` + `pynvml`).

**Lifecycle per tick:**

1. Report heartbeat + resource snapshot (`free_vram_gb`, `free_ram_gb`, `idle_cpus`, `unregistered_vram_gb`) to jobd every 5s.
2. Long-poll `/next-job?host=...&free_vram=...&free_ram=...&idle_cpus=...` (30s timeout).
3. On job received: `cd` to working dir, apply env, spawn child under `heavy-run`, stream stdout/stderr to jobd, poll `/jobs/<id>/signal` every 2s for cancel/preempt requests, wait for child exit, report final status.
4. On crash/restart: previously assigned jobs get marked `orphaned` by jobd after heartbeat gap; worker self-reports its state on restart; jobd reconciles.

**PID tracking:** worker captures the child PID and tracks all descendants. `nvidia-smi --query-compute-apps=pid,used_memory` readings are cross-referenced: any PID not in the worker's tracked set contributes to `unregistered_vram_gb`.

### job CLI

Thin HTTP client. Exit code reflects job exit code so Claude sees failures naturally.

```
job submit [--profile NAME] [--host HOST|any] [--priority DELTA]
           [--preemptible] [--cwd PATH] [--env KEY,KEY] [--wait]
           -- <cmd...>
job wait <id>                 # SSE stream of logs until terminal state
job list [--running|--queued|--mine|--project P]
job cancel <id>
job profiles [--show NAME]
job classify <cmd>            # "would this be routed? which profile?"
job fast -- <cmd>             # explicit fast-path hint
job projects list
job projects set P N          # edit projects.yaml + reload
job projects nudge P +N       # relative bump
job projects reset            # reload YAML, discard in-memory state
job bypass-review             # aggregate bypass log for classifier tuning
job classifier-promote <bypass_id> --profile P
```

### heavy-run wrapper (updated)

Your existing `~/.local/bin/heavy-run` grows a preamble. Behavior:

1. POST the incoming command to `jobd /classify`.
2. If classified heavy-gpu / heavy-ram with expected runtime above threshold, `exec job submit --profile <suggested> --wait -- <original cmd>`.
3. Else: fall through to existing cgroup-capped local execution.

Env flag `HEAVY_RUN_USE_QUEUE` gates this during rollout (Phase 3) — `=0` preserves current local-only behavior; `=1` enables queue routing; default flips to `=1` after a week of soak.

Fallback: if jobd is unreachable, `heavy-run` logs a loud warning and falls through to local cgroup execution. A broker outage should not block all work.

### Claude Code PreToolUse hook

Configured in `~/.claude/settings.json` globally. Intercepts Bash tool calls.

**Decision flow:**

1. Extract command string.
2. If starts with `job `, `heavy-run`, `HEAVY_OK=1`, or matches a built-in allowlist of obviously-light commands (git, ls, grep, rg, cat, head, tail, ruff, pyright, mypy, pytest without `--slow`/`-m gpu`, make without heavy targets), → permit.
3. Else POST the command to `jobd /classify`.
4. On `{heavy: true, confidence: high}` → block with a routing suggestion (`"This looks like sdxl-lora-train. Use: job submit --profile sdxl-lora-train -- <cmd>. To bypass: HEAVY_OK=1 <cmd>"`).
5. On `{heavy: true, confidence: medium}` → block with softer wording.
6. On `{heavy: true, confidence: low}` → permit + log to `classifier_hints` for review.
7. On `{heavy: false}` → permit.
8. On jobd unreachable → fail-open (permit) + log a warning. A broker outage should not block interactive work.

Start in `warn` mode during Phase 4 rollout (permits but announces "would block"); flip to `block` after a week of classifier tuning.

## Resource model

### Profile registry

`jobd/profiles.yaml`, loaded on startup, SIGHUP-reloaded. Starter set:

```yaml
profiles:
  # GPU-heavy
  sdxl-lora-train:
    vram_gb: 28
    ram_gb: 22
    cpus: 8
    expected_runtime: 8h
    preemptible: true
    host_hint: desktop
    requires: [cuda>=12.8]

  stylegan2-train:
    vram_gb: 18
    ram_gb: 16
    cpus: 6
    expected_runtime: 24h
    preemptible: true
    host_hint: desktop

  jepagame-sim:
    vram_gb: 8
    ram_gb: 4
    cpus: 4
    expected_runtime: 1h
    preemptible: false
    host_hint: desktop | laptop

  clip-audit:
    vram_gb: 6
    ram_gb: 8
    cpus: 4
    expected_runtime: 20m
    preemptible: true
    host_hint: any-gpu

  sdxl-inference-server:
    vram_gb: 14
    exclusive: true
    host_hint: desktop

  # CPU/RAM-heavy
  rnaseq-3worker:
    vram_gb: 0
    ram_gb: 24
    cpus: 12
    expected_runtime: 4h
    preemptible: false
    host_hint: desktop

  rnaseq-1worker:
    vram_gb: 0
    ram_gb: 10
    cpus: 3
    expected_runtime: 12h
    preemptible: false
    host_hint: any

  # Light
  scraper-small:
    vram_gb: 0
    ram_gb: 2
    cpus: 2
    expected_runtime: 1h
    preemptible: true
    host_hint: any
    fast_path: true

  cpu-quick:
    vram_gb: 0
    ram_gb: 1
    cpus: 1
    expected_runtime: 10m
    preemptible: true
    host_hint: local
    fast_path: true
```

Adding a profile is a one-file PR. Workers know nothing about profiles — they only see resolved `required_resources` on assigned jobs.

### Matcher fit check

Worker reports per 5s heartbeat:

- `free_vram_gb` — from `nvidia-smi --query-gpu=memory.free`
- `unregistered_vram_gb` — total used VRAM minus VRAM attributable to tracked child PIDs
- `free_ram_gb` — from `/proc/meminfo` MemAvailable
- `idle_cpus` — `nproc - load_avg_1min`

Matcher decides "can host X run job Y":

```
effective_free_vram = free_vram_gb - unregistered_vram_gb - safety_margin_vram_gb
fit_vram = job.vram_gb <= effective_free_vram
fit_ram  = job.ram_gb  <= free_ram_gb - safety_margin_ram_gb
fit_cpu  = job.cpus    <= idle_cpus + oversubscribe_allowance
host_ok  = job.host_pin == worker.host or job.host_pin in worker.host_aliases
```

`safety_margin_vram_gb = 1`, `safety_margin_ram_gb = 1`, `oversubscribe_allowance = 2` as v1 defaults. Tunable in `jobd/config.yaml`.

## Priority scheme

### Project table

`jobd/projects.yaml`, starter values reflecting the Tier triage in `projects_prioritization_2026-04-18.md` with phelipanche and arfd prioritized "somewhat" (10-point gap to Tier 2):

```yaml
projects:
  # Tier 1 — active academic obligations
  phelipanche: { priority: 80 }
  arfd-rnaseq: { priority: 80 }
  arf-genestruc: { priority: 75 }

  # Tier 2 — high-novelty research, decisions pending
  arf-promoter-analysis: { priority: 70 }
  dreamer-chassis: { priority: 70 }
  autosilhouette: { priority: 65 }

  # Tier 3 — active creative / hobby
  orchid-sdxl: { priority: 55 }
  jepagame: { priority: 55 }

  # Tier 4 — paused, needs fate decision
  agrigen: { priority: 30 }
  prompt2brick: { priority: 25 }

  # Tier 5 — ship or shelve
  seed3d: { priority: 20 }
  game-neuramon: { priority: 15 }

  # Unresolved — evo2 path discrepancy flagged in prioritization memory; verify before enabling
  evo2: { priority: 40 }

  _default: { priority: 40 }
```

**Tier spread rationale:**

- Tier 1 at 80 vs Tier 2 at 70 = 10-point gap = "somewhat" prioritized, not starving.
- Tier 3 at 55 vs Tier 4 at 30 = 25-point gap = paused projects yield clearly when active work queued.
- Tier 5 at 15-20 below `_default` (40) = shelf / experimental work genuinely goes last.
- Ad-hoc jobs (unknown project) land at `_default` (40), below all active work but above paused.

**Adjustability is the point:** these are starter values. Use `job projects set` / `nudge` as focus shifts. Tier boundaries in this doc are a snapshot of current thinking, not policy.

### Adjustability

- **YAML + SIGHUP:** edit the file in the repo, `systemctl kill -HUP jobd`, done. Changes tracked in git history.
- **CLI shortcuts:** `job projects set phelipanche 80`, `job projects nudge arfd-rnaseq +5`. These rewrite `projects.yaml` in-place, commit (optional flag), and POST `/reload`.
- **Per-job delta:** `job submit --priority +10 ...` boosts, `--priority -10` drops. Effective priority clamped to `[0, 100]`.
- **Tie-breaking:** FIFO by `submit_time`, ascending (older wins, newer yields).

## Classifier (extensibility is first-class)

### Rule format

`jobd/classifier.yaml`:

```yaml
rules:
  - id: sdxl-lora-train
    match:
      - command_regex: "^(bash\\s+)?train_lora_v\\d+\\.sh\\b"
      - command_regex: "^accelerate\\s+launch\\s+.*train_lora.*\\.py"
    suggest_profile: sdxl-lora-train
    confidence: high

  - id: stylegan2-train
    match:
      - command_regex: "python\\d?\\s+train\\.py\\s+.*--cfg"
      - command_contains: "stylegan2"
    suggest_profile: stylegan2-train
    confidence: high

  - id: jepagame-sim
    match:
      - command_regex: "\\./build/synthetic_souls\\b"
      - command_regex: "\\./build/ecology_audit\\b"
    suggest_profile: jepagame-sim
    confidence: high

  - id: rnaseq-pipeline
    match:
      - command_regex: "Rscript\\s+.*run_pipeline\\.R\\b"
      - command_regex: "snakemake\\s+.*rnaseq"
    suggest_profile: rnaseq-3worker
    host_aware: true
    confidence: high

  - id: heavy-pytest
    match:
      - command_regex: "pytest\\s+.*--slow"
      - command_regex: "pytest\\s+.*-m\\s+gpu"
    suggest_profile: clip-audit
    confidence: medium

  - id: dvc-repro
    match:
      - command_regex: "dvc\\s+repro\\b"
    suggest_profile: default-medium
    confidence: medium

  - id: ssh-desktop-heavy
    match:
      - command_regex: "ssh\\s+desktop(-wsl)?.*\\b(train_lora|run_pipeline|synthetic_souls)\\b"
    suggest_profile: auto
    confidence: medium
```

### Confidence ladder

- `high` → hook hard-blocks; bypass requires `HEAVY_OK=1`.
- `medium` → hook blocks with softer wording; `HEAVY_OK=1` still bypasses.
- `low` → hook permits + logs to `classifier_hints` for review.

### Feedback loop

Every `HEAVY_OK=1` bypass writes a row to `bypass_log`:

```
bypass_log:
  timestamp, session_id, project, cmd, cwd, exit_code,
  vram_consumed_peak_gb, ram_consumed_peak_gb, duration_seconds,
  host, reason_given, promoted_to_rule (nullable FK)
```

`job bypass-review` aggregates: per command prefix / frequency / average footprint / host distribution. `job classifier-promote <bypass_id> --profile X` writes a rule draft to a scratch area in the repo (`jobd/classifier.pending.yaml`) for the user to review + commit.

This is the mechanism that keeps the classifier in sync with real-world usage. The user's concern — "we may need to adapt the regex hook more as we find more heavy programs" — is explicitly designed for: the system surfaces unclassified heavy work and makes promoting it a near-zero-cost action.

### Extensibility guarantees

- **Hot-reloadable:** adding or editing rules requires a SIGHUP, not a restart.
- **Testable:** `tests/classifier_fixtures.yaml` has positive + negative examples for every rule. CI asserts each example matches as expected.
- **Discoverable:** `job classify <cmd>` shows which rule (if any) would match and why.
- **Safe default:** if no rule matches and no allowlist hits, classifier returns `{heavy: false}` — the hook fails open. New categories of heavy work appear first in bypass review, not as silent blocks.

## Data flow

### Happy path — long SDXL training

```
1. Claude: `job submit --profile sdxl-lora-train --wait -- bash train_lora_v5.sh`
2. CLI POSTs /submit {project: orchid-sdxl, cwd, cmd, profile, session_id}
3. jobd inserts row state=queued, priority=65 (orchid-sdxl default),
   host_pin=desktop (from profile.host_hint), preemptible=true.
   Returns {id: 142, state: queued, position: 2}.
4. CLI opens SSE to /wait/142.
5. desktop worker long-polls /next-job with its free-resource snapshot.
6. jobd matcher iterates queued jobs by priority DESC, submit_time ASC:
   - job 140 running (holds 28GB VRAM)
   - job 141 queued phelipanche CPU, priority 75 — can't fit
   - job 142 queued orchid-sdxl, priority 65 — needs 28GB VRAM, only 4 free
   Returns 204.
7. Job 140 completes, worker reports done.
8. desktop worker polls again; 32GB free. Matcher picks job 142.
9. Worker:
   - Marks job assigned
   - cd to cwd, spawns heavy-run --mem=22G --cpus=8 -- bash train_lora_v5.sh
   - Streams stdout/stderr to /jobs/142/log
   - Polls /jobs/142/signal every 2s
10. jobd relays logs through SSE to Claude.
11. Training finishes exit 0. Worker reports terminal status.
12. jobd marks completed, closes SSE with exit code, persists log.
13. CLI exits with code 0.
```

### Fast-path — light scraper

```
1. Claude: `python scripts/scrape_orchids.py --genus Cattleya`
2. Hook POSTs /classify. No rule matches → {heavy: false}.
3. Hook permits. Command runs directly in session. No queue interaction.
```

Fast-path for profile-declared `fast_path: true` jobs:

```
1. Claude: `job submit --profile scraper-small -- python scrape.py`
2. jobd checks: any worker has headroom right now? Yes (laptop idle).
3. jobd assigns directly to laptop worker, no queuing. Worker runs immediately.
   (Still registered — the GPU/RAM reservation is visible to the matcher
   for any concurrent heavier job that queues during the fast-path window.)
```

### Preemption path

```
1. Job 140 orchid-sdxl priority 65 preemptible running on desktop.
2. Claude submits job 150 evo2 priority 80 preemptible.
3. Matcher: 150 needs desktop GPU, held by 140.
   150.priority > 140.priority AND 140.preemptible → preempt candidate.
4. jobd marks 140 state=preempt-requested; worker receives signal.
5. Worker sends SIGTERM to training process. Grace window 60s.
6. Training checkpoints + exits. Worker reports preempted.
7. jobd requeues 140 (state=queued, submit_time preserved for FIFO).
8. Worker polls again; matcher assigns 150. Runs.
9. When 150 finishes, 140 picks up from checkpoint.
```

### Bypass path

```
1. Claude: `HEAVY_OK=1 python debug_diffusion.py --iters 50`
2. Hook sees bypass token, permits, POSTs to /bypass-log.
3. Command runs. GPU probe on host sees unregistered PID using VRAM.
4. jobd tags those GB as unregistered-busy; matcher routes around.
5. Later: `job bypass-review` surfaces it as a candidate rule.
```

### Failure paths

| Scenario                                             | Detection                               | Response                                                                                                                 |
| ---------------------------------------------------- | --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| Worker crashes mid-job                               | Missed heartbeats >30s                  | Job → `orphaned`, 2min grace, then requeue if profile.idempotent else `failed`                                           |
| Worker host reboots                                  | Heartbeat gap + systemd restart         | Same as crash; systemd auto-starts worker; worker self-reconciles on reconnect                                           |
| Job child hangs                                      | `expected_runtime * 3` exceeded         | Soft warning in SSE; hard timeout (`max_runtime: 48h` default) SIGKILLs                                                  |
| Job child OOMs                                       | Non-zero exit + heavy-run cgroup signal | Marked `failed` reason `oom`; surfaced in classifier review as undersized profile                                        |
| GPU probe stuck                                      | nvidia-smi >5s                          | Worker returns last-known with `stale_seconds`; matcher refuses new GPU jobs on that host until fresh                    |
| jobd down                                            | CLI/hook timeout                        | CLI: `--strict` fails; default fast-path falls through to local `heavy-run`, long jobs fail fast. Hook: fail-open + WARN |
| SQLite corruption                                    | Startup check fails                     | jobd refuses to start; Alertmanager alert via ntfy. Restore from 15-min cron backup                                      |
| Preempt target won't SIGTERM cleanly                 | Still running after grace               | Worker SIGKILLs; job marked `preempted-unclean`, NOT requeued (likely inconsistent state)                                |
| Claude session dies while `--wait`ing                | SSE closes                              | Job keeps running. `job wait <id>` reattaches. Dead watcher does not cancel                                              |
| Two workers race to claim same job                   | Concurrent `/next-job`                  | Prevented by `UPDATE ... WHERE state='queued'` SQL — second claimer gets 0 rows, retries                                 |
| Classifier false-positive                            | User frustration                        | `HEAVY_OK=1` bypass; bypass log surfaces for rule refinement                                                             |
| Classifier false-negative                            | GPU probe detects unregistered VRAM     | `bypass_log --auto` captures cmds using >Y VRAM without registration                                                     |
| Desktop-WSL worker auto-start not set up (known gap) | Worker never appears                    | Phase 0 prerequisite: fix this before jobd deployment                                                                    |

## Enforcement layers

The queue is only useful if Claude actually uses it. Four layers, defense in depth:

1. **`heavy-run` routes through jobd** — Claude already runs heavy commands through `heavy-run` per global CLAUDE.md. Updated `heavy-run` auto-submits when classifier flags the command. Claude doesn't have to learn a new command; the existing mandate now enforces cross-session coordination.
2. **PreToolUse hook blocks unwrapped heavy commands** — Bash tool calls that skip both `heavy-run` and `job` get blocked by the hook, with a routing suggestion. `HEAVY_OK=1` escape hatch for intentional bypass; bypass is logged.
3. **Per-project CLAUDE.md stubs** — each project's CLAUDE.md (orchid-sdxl, jepagame, phelipanche, evo2, arfd, autosilhouette) gets a "Running heavy jobs" section with canonical `job submit` examples. Discoverability layer; Claude reads CLAUDE.md at session start.
4. **GPU probe catches true bypass** — even when Claude SSHes to another host and runs raw, the worker's GPU probe detects unregistered VRAM. Other jobs route around automatically. Coordination survives non-compliance.

Known gap: the hook sees outer `ssh target "inner-cmd"` and cannot always classify the inner command. The classifier has rules for common patterns (`ssh\s+desktop.*train_lora`); the GPU probe catches what the classifier misses.

## Observability

### Prometheus metrics exposed by jobd

- `jobd_jobs_total{state, project, profile}` — counter
- `jobd_queue_depth{host, priority_tier}` — gauge
- `jobd_job_duration_seconds_bucket{profile, outcome}` — histogram
- `jobd_gpu_free_vram_gb{host}` — gauge
- `jobd_gpu_unregistered_vram_gb{host}` — gauge (key safety metric)
- `jobd_classifier_bypass_total{rule_id}` — counter
- `jobd_preemption_total{reason}` — counter
- `jobd_worker_last_heartbeat_seconds{host}` — gauge

Scraped by the existing Prometheus on gt76.

### Grafana dashboard

Ships with jobd as a JSON file in the GitOps repo (`jobd/grafana/dashboard.json`). Panels:

- Queue depth over time, stacked by priority tier
- Running jobs, by host, with VRAM/RAM utilization
- Unregistered VRAM per host (alerts if sustained >0 alongside queue-starvation)
- Per-project GPU hours over last 7 / 30 days
- Bypass volume, by classifier rule
- Preemption events, by reason

### Alertmanager rules → ntfy

- `JobdDown` — scrape fails for 5m
- `JobQueueStarved` — queue depth >5 for 30m (something is stuck)
- `JobdUnregisteredVramHigh` — unregistered VRAM >8GB for 15m on any host
- `JobdWorkerOffline` — worker heartbeat gap >5m
- `JobdSQLiteBloat` — DB size >1GB (archive sweep missed)

## Testing strategy

Small and focused. Enough to catch the scenarios that actually bite.

**Unit tests:**

- Matcher: priority tiebreak, FIFO tiebreak, host pin, resource fit, preemption trigger.
- Classifier: every rule in `classifier.yaml` has positive + negative fixtures; CI asserts.
- Profile loader: schema validation, SIGHUP reload, invalid profile rejection.
- Priority calc: project default + override, clamping, unknown project falls to `_default`.

**Integration tests (docker-compose):**

- jobd + mock-GPU worker. Submit, wait, observe, cancel. Assert DB transitions.
- Two workers contending. Matcher assigns correctly.
- Worker crash mid-job. Orphan + requeue.
- jobd restart mid-job. Worker reattaches cleanly.
- Preemption with a fake `preemptible: true` job that traps SIGTERM + exits.

**End-to-end on real homelab (manual, before Phase 3+ cutover):**

- Fast job submitted from laptop → runs on any host.
- GPU-pinned job → waits if desktop busy, runs when free.
- Manual GPU launch bypassing jobd → GPU probe detects → queued job routes elsewhere.
- jobd killed mid-run → fast-path falls through, long jobs fail fast.
- Two Claude sessions contending → FIFO-with-priority enforced.

## Rollout plan

Phased, reversible, weeks of wall time between phases.

### Phase 0 — prerequisites

- Fix desktop-WSL Ray/worker auto-start (`homelab.md` Next Steps).
- Extract canonical heavy-command list from each project's CLAUDE.md; seed `classifier.yaml`.
- Confirm Tailscale HTTP reliability between hosts.
- Decide SQLite or Postgres start (spec: SQLite).

### Phase 1 — skeleton (shadow mode)

- Build jobd with `/submit`, `/status`, `/wait`, `/classify`. Schema, priority, profile loader, classifier loader.
- Build `job-worker` for desktop only. systemd user unit.
- Build `job` CLI. Install on laptop.
- Deploy. Manual exercise: `job submit --profile clip-audit -- python scripts/audit_v7.py`.
- No hook, no heavy-run integration.
- Success: 5 real heavy jobs submitted + completed end-to-end without issues.

### Phase 2 — GPU probe + workers on all hosts

- Deploy workers to laptop and gt76.
- Wire up GPU probe; verify unregistered VRAM detection.
- Add preemption path; test with toy preemptible job.
- Success: conflicting jobs route correctly; GPU probe stable for a week.

### Phase 3 — heavy-run integration (opt-in)

- Update `heavy-run` to consult jobd `/classify` behind `HEAVY_RUN_USE_QUEUE=1`.
- Run with flag for a week; debug edge cases.
- Flip flag to default-on.

### Phase 4 — PreToolUse hook (warn → block)

- Deploy hook to `~/.claude/settings.json` globally in `warn` mode.
- Review `bypass_log` and `classifier_hints` daily; refine `classifier.yaml`.
- Flip to `block` after a week.

### Phase 5 — observability + polish

- Grafana dashboard.
- Alertmanager rules.
- `job bypass-review` + `job classifier-promote` tooling.
- Per-project CLAUDE.md stubs.
- GitOps README for jobd.

### Phase 6 — iterate on lived experience

- Tune priorities as focus shifts.
- Add profiles as workloads emerge.
- Promote frequent bypasses.
- Revisit Postgres / web UI / Nomad only if real pressure warrants.

## Open questions

1. **Separate `jobd` repo vs `~/homelab/jobd/` subdir on gt76.** Spec assumes separate repo. Tradeoff: separate = cleaner boundary, release independently; subdir = one repo to clone, shared infra. User preference is "clean repo early" → separate repo.
2. **Idempotency declaration per profile.** Currently implicit via `preemptible`. Should idempotency be explicit (a `retry_on_crash: true` flag) so non-preemptible-but-safe-to-retry jobs can be requeued after worker crash? Probably yes, deferred to Phase 1 detailed design.
3. **Env forwarding policy.** jobd strips env by default, `--env-pass FOO,BAR` forwards specific vars. Is there a project-wide secret pattern (e.g. DVC MinIO creds) that should be in a jobd-managed default allowlist? Determined during Phase 1 in consultation with existing secret patterns.
4. **Worker user.** Spec assumes jobs run as the local `mjarnold` user. Multi-user scenarios (another account on the desktop) deferred to future work.
5. **Fast-path recursion.** If a fast-path job itself calls `heavy-run`, does the inner call re-classify? Probably yes (keeps uniform behavior), but risks infinite bounce if misconfigured. Phase 1 integration test for this case.
6. **evo2 project verification.** `projects_prioritization_2026-04-18.md` flags a discrepancy: `projects.md` memory describes evo2 as the StripedHyena 2 DNA language model, but filesystem survey found a `meme-5.4.1` source tree at `/home/mjarnold/workspace/evo2`. Spec includes `evo2` in the priority table at the default tier pending verification. Resolve before Phase 1 ships (verify path + actual project), then either confirm the entry or remove it.
7. **Ray coexistence.** Ray head is already running on gt76 (port 6379/8265). jobd does not use or replace Ray. For Python-native jobs that benefit from Ray's distributed compute, a job can `ray.init(...)` inside its command — jobd schedules the outer process, Ray handles the inner parallelism. Explicit coexistence policy, not a contradiction.
8. **Heavy-run migration path.** Phase 3 modifies the user's existing `~/.local/bin/heavy-run`. The current script is already cgroup-based. Need to preserve: `HEAVY_RUN_MEM` / `HEAVY_RUN_SWAP` overrides, the systemd user-scope invocation, earlyoom as last-resort. Migration: back up current heavy-run, ship new one behind `HEAVY_RUN_USE_QUEUE` flag (default `0` for one week), flip default to `1`, keep old script as `heavy-run-local` for emergency fallback.
