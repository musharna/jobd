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

```bash
# Docker
docker compose pull && docker compose up -d
# systemd
systemctl --user restart jobd-broker
job ping        # confirm the new version is serving
```

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

By default the broker keeps every job row and per-job `.log` forever. Set
`JOBD_JOB_RETENTION_DAYS=N` to have the sweeper delete terminal jobs (and their
`.log` files) whose `finished_at` is older than `N` days, bounding jobs-table and
log-dir growth on a long-running broker. It emits a `jobs_pruned` event per pass
that removes anything. Leave it unset (or `0`) to disable. The SQLite file
reuses freed pages under WAL, so it stays bounded by the retention window
without a `VACUUM`; if you need to reclaim file _size_ after a large one-time
purge, run `VACUUM` manually during a maintenance window (it takes a global
lock).

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
