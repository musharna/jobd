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

Workers handle `SIGTERM` gracefully: the daemon stops claiming new jobs and
emits a `worker_shutdown` event. In-flight jobs are NOT killed by the stop — let
them finish, or `job preempt`/`job cancel` them first.

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

## Rotate the auth token

See [security.md](security.md) — rotation is a coordinated push (broker first,
then every worker unit and CLI/MCP wrapper). There's no overlap window; workers
401 against the new broker until updated. Do it during a quiet moment.

## Back up / restore the database

The broker runs SQLite in WAL mode, so use the online-backup API rather than a
raw `cp` (which can miss the `-wal`/`-shm` sidecar files):

```bash
# Online backup — safe while the broker is running:
sqlite3 /app/data/jobd.db ".backup '/backups/jobd-backup.db'"

# Restore: stop the broker, replace the file, restart.
systemctl --user stop jobd-broker        # or: docker compose stop
cp /backups/jobd-backup.db /app/data/jobd.db
systemctl --user start jobd-broker
```

Per-job logs live under `JOBD_LOGS_DIR` and the event stream in
`events.jsonl`; back those up too if you need an audit trail across a rebuild.

## Known issues

- **A GPU worker may report its own running job as "unregistered" (foreign)
  VRAM.** For systemd-scope-wrapped jobs the worker tracks the `systemd-run`
  client pid rather than the in-scope workload pid, so a worker's own GPU job
  inflates the heartbeat's `unregistered_vram_gb` and can trigger spurious
  submit-time contention warnings. This is a reporting artifact only — the
  routing-critical `free_vram` figure is computed from in-flight allocations and
  stays correct, so jobs still place properly. At `JOBD_WORKER_MAX_CONCURRENT_JOBS
  > 1` it can also make a worker refuse its own second GPU job. Fix is tracked
  > for a dedicated GPU-host pass.
