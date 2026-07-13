# Operator runbook

Day-2 operations for a running jobd deployment. All commands assume the `job`
CLI is configured (`JOBD_URL` + `JOBD_API_TOKEN` in its environment).

## Is the fleet healthy?

```bash
job ping        # broker reachability + latency + version; exits 0 if healthy
job workers     # per-worker snapshot: state, last heartbeat, free VRAM/RAM/CPU
```

A worker silent past the stale threshold is shown as `stale` (matcher stops
dispatching to it) and, later, `offline`. `job ping` reports the broker's
version — handy for confirming which build is live after an upgrade.

## Diagnose a stuck or failed job

```bash
job status ID          # full state, worker, timing, warnings
job logs ID            # tail of captured stdout/stderr
job audit --since 24h  # broker event history (dispatch, admission, timeouts, …)
job gpu-holders        # which PIDs hold VRAM on each GPU worker
```

A job stuck in `queued` usually means nothing matches its requirements — check
`job status` for a `warning:` (e.g. unsatisfiable `needs:` tag or VRAM ask), and
`job workers` for capacity. A job in `running` whose worker has died is reclaimed
automatically (idempotent jobs requeue; others become `orphaned`) once the
worker's heartbeat passes the dead-worker cutoff.

## Drain / restart a worker

Workers handle `SIGTERM` with a full drain (docs/plans/sigterm-drain.md): the
daemon stops claiming new jobs, preempts every in-flight job (SIGTERM to the
workload, checkpoint window honored, SIGKILL after the grace), waits for each
to post `/complete`, then emits a `worker_shutdown` event with a
`signaled`/`aborted` summary and exits 0. Drained jobs land as `preempted`
with `termination_reason=worker_shutdown` — resume them via the normal
checkpoint convention (docs/preemption.md).

Per-job grace during a drain is `min(checkpoint_grace_s, JOBD_WORKER_DRAIN_GRACE_S)`
(default 60 s). If you raise `JOBD_WORKER_DRAIN_GRACE_S`, raise
`TimeoutStopSec` in job-worker.service in step, or systemd will SIGKILL the
worker mid-drain. Expect `systemctl stop` to take up to ~35 s before the drain
even starts (the worker may be inside a `/next-job` long-poll).

If a worker dies WITHOUT draining (SIGKILL, crash, power loss), the broker's
heartbeat reconcile cleans up: the restarted worker reports its (empty)
in-flight set, and after 2 consecutive heartbeats (~10 s) any stranded claim
older than 60 s is requeued (ASSIGNED, or RUNNING + idempotent) or orphaned
with `termination_reason=worker_restarted` (RUNNING, non-idempotent).

```bash
systemctl --user stop job-worker        # graceful drain (SIGTERM)
# … do maintenance …
systemctl --user start job-worker
job delete-worker HOST                  # purge a registration that won't return
```

## Restart / upgrade the broker

Broker state lives in SQLite, so a restart is safe: `queued`/`assigned` jobs are
preserved, and `running` jobs survive because their workers keep streaming and
post `/complete` to the broker once it's back. Schema migrations are
additive-only and run automatically at startup (`migrate()` in `src/jobd/db.py`).

**You normally don't.** A `jobd-deploy.timer` on the broker host checks every 5
minutes for a newly published release and deploys it — pinning the exact version,
restarting, and verifying the broker comes back reporting that version, rolling back
to the previous pin if it doesn't. See "Continuous deployment" below.

To deploy right now, or to deploy a specific version (which is also how you roll
back):

```bash
sudo jobd-deploy            # newest published release
sudo jobd-deploy 0.5.16     # an exact version — this is the rollback path
DRY_RUN=1 sudo jobd-deploy  # say what it would do, change nothing
job ping                    # confirm the version that is actually serving
```

By hand, without the script:

```bash
# Pin the version you want in .env, then pull and restart.
sed -i 's/^JOBD_TAG=.*/JOBD_TAG=0.5.17/' .env
docker compose pull && docker compose up -d
job ping        # confirm the new version is serving — do not skip this
```

**Why there is no `--build` any more.** There used to be, and it was load-bearing:
compose built the broker from source (`build: .`) and tagged it `jobd:latest` with
**no registry behind that tag**, so `docker compose pull` was a silent no-op and a
bare `up -d` found a `jobd:latest` already present and reused it — restarting the
*old* code after a `git pull`, with nothing to tell you. Remembering `--build`
forever was a guard against the footgun rather than removal of it.

Compose now pulls a **version-pinned image from GHCR** (`JOBD_TAG` in `.env`). That
deletes the class: `pull` means something, the running version is knowable and
pinned, and rollback is possible at all. `tests/test_deploy_lint.py` fails if a
`build:` stanza reappears in `docker-compose.yml`.

Building from source (a fork, or a local change) is still supported, deliberately:

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

## Continuous deployment

Tagging `vX.Y.Z` publishes to PyPI, cuts a GitHub release, and pushes
`ghcr.io/musharna/jobd:X.Y.Z`. The broker host then picks it up on its own.

It is **pull-based**: gt76 has no public ingress, so a push-based CD would mean
storing a tailnet auth key and an SSH key in GitHub and letting CI reach into the
homelab. Nothing in this path needs a secret, and nothing outside the network can
trigger a deploy.

```bash
# One-time install on the broker host:
sudo cp scripts/deploy-broker.sh /usr/local/bin/jobd-deploy && sudo chmod +x /usr/local/bin/jobd-deploy
sudo cp scripts/jobd-deploy.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now jobd-deploy.timer

systemctl list-timers jobd-deploy.timer     # when it next fires
journalctl -u jobd-deploy.service -n 50     # what it did last time
```

The deploy **verifies itself**: after restarting, it asks the broker its version and
only declares success if the broker reports the version that was deployed. If it
doesn't within 90s, the previous pin is restored. It also writes
`jobd_deploy_last_run_success` / `jobd_deploy_running_version_info` as node-exporter
textfile metrics, so a failed deploy is visible in Prometheus rather than only in a
journal nobody reads.

One-time GHCR note: the package must be **public** for the host to pull it without
credentials (Packages → jobd → Package settings → Change visibility). The repo is
already public; this just keeps the deploy path credential-free.

**Broker crash-loops on `attempt to write a readonly database`?** The image runs as a
non-root user (uid 10001); a bind-mounted `data/`/`logs/` owned by a different host uid
isn't writable by it. Fix: either `chown -R 10001:10001 ./data ./logs`, or run the
container as your host user (`user: "$(id -u):$(id -g)"` in compose — keeps the files
host-owned for backups). See the commented `user:` block in `docker-compose.yml`.

**systemd worker fails `can't open file '.../worker/job_worker.py'`?** The unit's
`ExecStart` hardcodes a source path that a refactor moved. Point it at the installed
console script instead — `ExecStart=…/.venv/bin/jobd-worker` — which is layout-stable.

## Rotate the auth token

See [security.md](security.md) — rotation is a coordinated push (broker first,
then every worker unit and CLI/MCP wrapper). There's no overlap window; workers
401 against the new broker until updated. Do it during a quiet moment.

## Config vs state: who owns what

Two different things live in two different places, and the split matters:

| | Where | Owner | Mutable at runtime? |
|---|---|---|---|
| **Config** — projects, their baseline priority + `defaults:`, profiles, classifier | `$JOBD_CONFIG_DIR` (`./config`, bind-mounted **`:ro`**) | **git** | No |
| **State** — runtime priority overrides from `job projects set` / `nudge` | `$JOBD_STATE_DIR/project-priorities.yaml` (defaults to the SQLite DB's directory, `./data`) | **the broker** | Yes |

Why: `job projects set` / `nudge` used to rewrite `config/projects.yaml` in place.
That file is git-owned *and* mounted read-only, so every call raised
`OSError: Read-only file system` → **HTTP 500** — the feature was dead in
production (audit 2026-07-12). Splitting ownership fixes the cause rather than
widening the mount, and gives three properties:

- **A redeploy never fights runtime state.** `git merge --ff-only` can't conflict
  with a file the broker writes, because it doesn't write one.
- **The overlay stores only *deltas* from the git baseline.** So a config-as-code
  priority change still takes effect for any project nobody nudged, while a
  nudged project keeps its override. `POST /reload` re-reads both layers.
- **`defaults:` blocks are git-only.** The endpoints only ever touch `priority`,
  so the old "one nudge silently erases every `defaults:` block" hazard is now
  structurally impossible rather than merely tested against.

The overlay lives next to the DB on purpose: it is state, so the DB backup below
already covers it.

## Back up / restore the database

The broker runs SQLite in WAL mode, so use the online-backup API rather than a
raw `cp` (which can miss the `-wal`/`-shm` sidecar files):

```bash
# Online backup — safe while the broker is running:
sqlite3 /app/data/jobd.db ".backup '/backups/jobd-backup.db'"

# The runtime priority overlay is state, not config — back it up with the DB:
cp /app/data/project-priorities.yaml /backups/

# Restore: stop the broker, replace the file, restart.
systemctl --user stop jobd-broker        # or: docker compose stop
cp /backups/jobd-backup.db /app/data/jobd.db
systemctl --user start jobd-broker
```

Per-job logs live under `JOBD_LOGS_DIR` and the event stream in
`events.jsonl`; back those up too if you need an audit trail across a rebuild.

### Event-stream retention

`events.jsonl` is size-rotated: when it crosses `JOBD_EVENTS_MAX_BYTES`
(default 50 MB) the broker moves it to `events.jsonl.1` (one backup, overwriting
any prior one) and starts a fresh file. Total on-disk retention is therefore
~2× the threshold; `job audit` / `GET /events` read both files, so a query
spanning a rotation still returns a continuous history within that window. Raise
`JOBD_EVENTS_MAX_BYTES` if you need a longer audit trail on the broker itself,
or back the files up out-of-band for unbounded history.

### Job/log retention

Job **rows** and job **logs** prune on two independent clocks, because their
cost/value profiles are opposite. Measured on the live broker (2026-07-12):

| | Size | Growth | Value over time |
|---|---|---|---|
| 2,875 job **rows** | **6.4 MB** | ~7 MB/yr | **rises** — feeds the ETA estimator's per-project p50/p90 |
| 2,605 job **logs** | **2.0 GB** | **~0.7 GB/mo** | **falls** — write-once-read-maybe; read while debugging, then never |

A single shared clock forced a false choice — keep 8 GB/yr of logs, or discard
the cheap history the estimator runs on — which is why retention used to be off
entirely. Two knobs remove the choice:

- **`JOBD_LOG_RETENTION_DAYS`** (default **60**) — unlink the `.log` of a terminal
  job finished more than N days ago, **keeping the row**. This is the one that
  bounds disk. Emits `logs_pruned`. `0` disables.
- **`JOBD_JOB_RETENTION_DAYS`** (default **0** = keep forever) — delete the terminal
  **row** itself (and its log) after N days. Rows are cheap and get more useful
  with age, so this stays opt-in. Emits `jobs_pruned`.

A job whose log was pruned reports `pruned: true` from `/jobs/{id}/output`, and
`job logs` says so explicitly — a pruned log must never be confused with "the
job produced no output".

Each job is stamped `log_pruned_at` once its log is dealt with, so the prune scan
shrinks monotonically instead of re-stat'ing all of history every 30s sweep.

The SQLite file reuses freed pages under WAL, so it stays bounded by the
retention window without a `VACUUM`; if you need to reclaim file _size_ after a
large one-time purge, run `VACUUM` manually during a maintenance window (it takes
a global lock).

### Worker polling / dispatch latency

Workers long-poll `/next-job`: the broker holds the request until a job is
dispatchable (woken by a submit, a terminal transition, or a requeue) or until
the worker's poll timeout (~30s) elapses, then the worker re-polls. An idle
worker therefore makes almost no requests while waiting, and a freshly submitted
job is picked up near-instantly rather than on the next poll tick. This needs no
configuration. A worker pointed at an older broker that doesn't support the hold
falls back to a 2s re-poll automatically. If you run the broker behind a reverse
proxy, make sure its idle/read timeout exceeds ~35s so it doesn't sever the held
connection.

# Troubleshooting

## `job` (or jobd / jobd-mcp / jobd-worker) throws `ModuleNotFoundError: No module named 'job_cli'`

The canonical CLI lives in the install venv (`~/jobd/.venv/bin/job`), but a stray
`pip install jobd` into another interpreter (commonly base conda,
`~/miniconda3/bin`) can leave a broken entry-point shim there. If that dir precedes
`~/jobd/.venv/bin` on `PATH`, the broken shim wins and fails to import `job_cli`.

Confirm: `command -v job` points at the wrong dir, and
`~/jobd/.venv/bin/job workers` works while `job workers` does not.

Fix (idempotent): `scripts/fix-cli-shims.sh` — backs up each shadowing broken shim
and symlinks it to the canonical venv entry point. Set `JOBD_VENV_BIN` if the venv
is elsewhere. Run `hash -r` (or open a new shell) afterwards — an already-running
shell may have the old broken path cached. Alternatively, put `~/jobd/.venv/bin`
ahead of the offending dir on `PATH`, or `pip uninstall jobd` from the interpreter
that shouldn't have it.
