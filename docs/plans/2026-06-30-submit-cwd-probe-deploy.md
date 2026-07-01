# Deploy checklist — submit cwd probe + worker cwd re-queue

> Branch `feat/submit-cwd-probe`. Multi-host: broker (gt76, docker) + 4 workers
> (laptop, desktop, gt76, msi-4080). All changes are additive/back-compat, so
> deploy order is forgiving; the recommended order below minimizes the window
> where a new worker talks to an old broker.

## What changed (deploy-relevant)

- **Schema:** new `jobs.excluded_workers_json` column. **Self-migrating** — it is
  registered in `_JOB_ADDS` (`src/jobd/db.py`), so `migrate(engine)` adds it
  idempotently on broker startup (`ALTER TABLE jobs ADD COLUMN
excluded_workers_json TEXT DEFAULT '[]'`). **No manual SQL needed.**
- **Broker:** `cwd_routability` submit check (A); `refuse-admission` `cwd_missing`
  branch + `/next-job` exclusion filter (B). `app.py`, `matcher.py`, `db.py`,
  `models.py`.
- **Worker:** `os.path.isdir(cwd)` pre-dispatch check → `refuse-admission`
  `cwd_missing`. `worker/job_worker.py`.

## Back-compat (why order is forgiving)

- New `AdmissionRefusal.reason`/`cwd` default to the GPU path; old broker ignores
  unknown fields only if the worker sends them — an **old worker → new broker**
  is fine (worker never sends `cwd_missing`, broker's default branch runs).
- A **new worker → old broker**: the new worker may POST `reason=cwd_missing`; an
  old broker's `AdmissionRefusal` lacks `reason`/`cwd` and **requires**
  `required_gb`/`free_gb` — the POST would 422. So **deploy the broker before any
  new worker** to avoid that window.
- `WorkerSnapshot.mount_roots` and `excluded_workers_json` default empty/`[]`.

## Order

1. **Broker first (gt76).** Pull `feat/submit-cwd-probe` (or the merge to the
   deployed line) into the gt76 broker checkout; rebuild/restart the
   `jobd-broker` container. On startup `migrate()` adds the column. Verify:
   `job workers` returns; submit a trivial job; `sqlite3 <db> '.schema jobs'`
   shows `excluded_workers_json`.
2. **Workers (laptop, desktop, gt76, msi-4080).** Update each host's checkout and
   restart its worker. The **laptop** is an editable install
   (`/home/mjarnold/jobd/src` via `_editable_impl_jobd.pth`) — merging to the
   deployed branch + `systemctl --user restart job-worker.service` picks up
   `src/` directly. Desktop/gt76/msi: update their `~/jobd-worker/` (or
   equivalent) checkout, restart the worker.

## Real-execution check (run after deploy — the multi-host boundary tests can't cover)

From the laptop, in a git worktree (a host-local path under `/home`):

```bash
cd /home/mjarnold/jepagame/.claude/worktrees/<some-worktree>
job submit --project jepagame --cwd $(pwd) --gpu --needs cuda-24gb --wait -- \
  bash -lc 'echo HOST=$(hostname); pwd'
```

Expected (new behavior): the job either (a) runs on `laptop` (`HOST=SCAR18`) —
if a remote worker claimed it first, it refuses `cwd_missing`, is excluded, and
re-routes to laptop — or (b) if no worker has the path, fails fast with
`termination_reason=cwd_unreachable` and a `job logs` line naming the cwd. It
must NOT silently `exit 127` after a remote misroute (the old behavior).

Also confirm the A-path: `job submit --host desktop --cwd /mnt/d/only-on-laptop ...`
(a path desktop doesn't advertise) → submit 400s with a routable hint.

## Rollback

Revert the worker first (so no worker emits `cwd_missing`), then the broker. The
`excluded_workers_json` column is harmless if unused (reads `[]`); leaving it in
place on rollback is fine — no down-migration needed.
