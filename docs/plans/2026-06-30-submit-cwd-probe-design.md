# Submit-time cwd probe + worker-side cwd re-queue — design spec

> 2026-06-30 · branch `feat/submit-cwd-probe` (off `public`/v0.5.5) · fixes the
> silent-misroute class where a job's `cwd` exists only on some hosts but routes
> to one that lacks it and dies `exit 127` minutes later.

## Problem

A job's `cwd` can be **host-local** (exists on one worker's filesystem, not
others). The broker infers routability from each worker's reported `mount_roots`
(coarse directory prefixes: `/home`, `/tmp`, `/mnt/c`, …). At `/next-job` the
broker filters jobs whose `cwd` doesn't `startswith` any of the polling worker's
`mount_roots` (`app.py:1172`).

This is **necessary but insufficient**: a path like
`/home/mjarnold/jepagame/.claude/worktrees/embodiment-v8` (a git worktree, which
is never cloned to other hosts) is under `/home`, and **every** worker advertises
`/home`. So the prefix filter passes the job to a remote worker whose `/home`
does not contain that worktree → the worker `cd`s, fails, and reports
`[worker setup error] No such file or directory` / `exit_code=127`
(`job_worker.py:914` Popen `cwd=`, OSError path). The user only learns minutes
later, after the job was (mis)dispatched.

Live incident (2026-06-30): jepagame GPU test job submitted from a worktree cwd
with `--gpu` (`host_pin=any`) routed to a non-laptop worker and died 127.

`mount_roots` is a **prefix-capability** claim, not a **filesystem-identity**
claim; the broker assumes `/home` content is identical across hosts. It isn't.

## Goal

Fail fast with a routable signal instead of a silent misroute, AND make a
host-local-path job actually reach the host that has the path. Two layers:

- **A (submit-time prefix probe):** at `/submit`, catch cwds that **no known
  worker** could reach, or that a **pinned host** can't — a generalization of the
  existing `/mnt/c` guard. Cheap; catches typo cwds, pinned-host-without-mount,
  and paths on no host. Does **not** catch the worktree-under-`/home` case (every
  worker claims `/home`) — that's B's job.
- **B (worker-side pre-dispatch cwd check + re-queue):** the claiming worker
  verifies `os.path.isdir(cwd)` before running; if missing it **refuses
  admission** (re-queues) rather than failing 127, and the broker excludes that
  worker for that job so it re-routes to a host that has the path — terminating
  cleanly if no host does.

## Non-goals (named, deferred)

- Per-worker filesystem/host UUID + a shared-vs-host-local mount taxonomy (the
  "most correct" fix). Out of scope; `mount_roots` prefixes + B's runtime probe
  cover the real cases.
- A synchronous broker→worker "do you have this cwd?" RPC. **Impossible** under
  the current pull-only protocol (workers poll `/next-job`; the broker never
  calls a worker). B uses the existing worker-initiated refuse-admission path
  instead.
- Reporting a full per-worker cwd inventory in the heartbeat (expensive,
  unnecessary).

## A — submit-time prefix probe (broker)

**Thread `mount_roots` onto the snapshot.** `WorkerSnapshot` (`matcher.py:25`)
has no `mount_roots`; add `mount_roots: list[str] = field(default_factory=list)`.
Populate it in `_build_snapshots` (`app.py`) from `worker.mount_roots_json` (the
column the heartbeat already writes, `app.py:1053`).

**New pure function `cwd_routability(cwd, host_pin, all_known_workers) -> str | None`**
in `matcher.py`, sibling of `submit_preflight` (`matcher.py:218`):

- A worker **covers** `cwd` iff it has a non-empty `mount_roots` and some root is
  a prefix of `cwd` (`any(cwd.startswith(r) for r in w.mount_roots)`).
- A worker with **empty** `mount_roots` is **unknown** (old worker that doesn't
  advertise) — never counts as "can't cover"; it is excluded from the deny
  decision so we never false-reject on missing data.
- `host_pin != "any"`: restrict to workers matching the pin (host or alias). If
  **at least one** such worker has non-empty `mount_roots` AND **none** of the
  pinned workers cover `cwd` → return a hard-deny string (caller raises 400 with
  a routable hint). If all pinned workers have empty `mount_roots` → return None
  (unknown, don't block).
- `host_pin == "any"`: if **at least one** known worker has non-empty
  `mount_roots` AND **no** known worker covers `cwd` → return a warning string
  (NOT a hard 400 — `any` is best-effort and B is the safety net). Else None.

**Wire into `/submit`** (`app.py`, near the `submit_preflight` call at `:447`):

- Keep the existing `/mnt/c` fast 400 (`:419`) verbatim — specific, correct,
  cheap; `cwd_routability` is the general layer beneath it.
- Compute `cwd_routability`. If it returns a **hard-deny** (pinned host can't
  cover): `raise HTTPException(400, detail=...)` with a hint
  (`"cwd {cwd!r} is not under any mount_root of host_pin={pin!r} (roots: …). Pass
--host <a-host-that-has-it>, or stage under a shared path."`). If it returns a
  **warning** (any-pin, no coverage): fold into the existing `warnings` list
  (`:448`) — surfaced, not blocking.

Distinguish hard-deny vs warning by return type: return a small dataclass
`CwdRouting(deny: bool, message: str)` or two functions. (Plan picks one;
default: one function returning `tuple[bool, str] | None` — `(is_hard, msg)`.)

## B — worker-side pre-dispatch cwd check + re-queue (worker + broker)

**Worker (`job_worker.py run_job`, immediately before the launcher check at
`:819`):**

```python
if not os.path.isdir(cwd):
    # Host-local cwd absent here (e.g. a git worktree that lives only on
    # another host). Refuse admission so the broker re-routes to a host that
    # has it, instead of cd-failing to exit 127. The broker excludes this
    # worker for this job to prevent a re-claim loop.
    try:
        client.post(f"/jobs/{job_id}/refuse-admission",
                    json={"reason": "cwd_missing", "cwd": cwd},
                    timeout=10.0)
    except Exception as e:
        print(f"[worker] cwd-missing refuse post failed for job {job_id}: {e}",
              file=sys.stderr)
    return
```

Mirror the existing refuse-admission call site (`job_worker.py:1416`). The
`os.path.isdir` import is already in use (`:616`).

**Broker — extend `/jobs/{job_id}/refuse-admission`** (`app.py:913`) to accept
the existing GPU-contention `AdmissionRefusal` **and** a cwd-missing refusal.
Add optional fields to `AdmissionRefusal` (`models.py:301`): `reason: str =
"gpu_contention"`, `cwd: str | None = None`. Behavior split inside the handler:

- Reason `cwd_missing`: revert to QUEUED (as today) AND **record the refusing
  worker in the job's exclusion set** (new `excluded_workers_json` column on
  `Job`, nullable TEXT, JSON list). Emit a `cwd_refused` event. Then: re-derive
  the job's **eligible workers minus excluded**; if that set is **empty**, mark
  the job `failed` with `termination_reason="cwd_unreachable"` and a log line
  (`"cwd {cwd} exists on no eligible worker (excluded: …)"`) instead of leaving
  it QUEUED forever. Else `_wake_dispatchers()` to re-route.
- Reason `gpu_contention` (default / absent): unchanged behavior.

**Broker — `/next-job` exclusion filter** (`app.py:1172`, beside the mount_roots
filter): drop jobs where the polling worker (`q.host`) is in
`excluded_workers_json`. One added clause:
`queued = [j for j in queued if q.host not in _excluded(j)]`.

**Schema.** `excluded_workers_json TEXT NULL` on `Job`. Confirm the DB init path
(SQLAlchemy `create_all` additive vs a migration step) in the plan; a nullable
column with a `None`→`[]` read default is back-compatible (old rows read empty).

**Loop bound.** Each worker refuses a given job at most once (it's then
excluded), so the job visits each eligible host ≤1×; with K eligible workers the
job is re-queued ≤K times before it either lands on a host with the cwd or
terminates `cwd_unreachable`. No hot loop.

## Decisive tests (mirror `tests/test_api.py` / `tests/test_matcher.py`)

1. **A unit (`test_matcher.py`):** `cwd_routability` —
   (a) pinned host whose mount_roots don't cover cwd → hard-deny;
   (b) any-pin, no worker covers cwd, some worker has non-empty roots → warning;
   (c) any-pin, a worker covers cwd → None;
   (d) all workers have empty mount_roots → None (no false reject);
   (e) `/home`-everywhere + worktree cwd → **None** (A deliberately does NOT
   catch this — documents the A/B split).
2. **A integration (`test_api.py`, mirror
   `test_submit_rejects_mnt_c_cwd_for_non_laptop` at `:1413`):** submit with a
   pinned host lacking the cwd mount_root → 400 with routable hint; submit
   any-pin with no covering worker → 200 + warning string present.
3. **B exclusion (`test_api.py`):** simulate worker W1 claiming a job
   (state→ASSIGNED), POST refuse-admission `reason=cwd_missing` → job back to
   QUEUED, W1 in `excluded_workers`; a `/next-job` poll as W1 does **not**
   re-offer the job; a poll as W2 (covers cwd) **does**.
4. **B terminal:** a job whose only eligible worker refuses cwd_missing →
   job `failed`, `termination_reason="cwd_unreachable"` (not stuck QUEUED).
5. **Back-compat:** existing `refuse-admission` GPU-contention test still passes
   (default reason, no cwd, no exclusion recorded); existing mount_roots
   `/next-job` filter test unchanged.

## Real-execution check (per testing doctrine)

After unit/integration green: a live shell walkthrough against the running fleet
— submit the exact worktree cwd with `--gpu` (no `--host`), confirm it either
re-routes to `laptop` and runs, or fails `cwd_unreachable` with the hint (rather
than the old silent 127). This is the boundary the synthetic tests can't cover
(real multi-host mount_roots).

## Byte-floor / back-compat

- `AdmissionRefusal` new fields default to the current behavior (`reason=
"gpu_contention"`, `cwd=None`) → existing callers unchanged.
- `WorkerSnapshot.mount_roots` defaults to `[]` → existing matcher logic
  unaffected (only the new `cwd_routability` reads it).
- `excluded_workers_json` nullable, read as `[]` when NULL → old jobs unaffected.
- Old workers (empty mount_roots, no cwd-check) keep working: A treats them as
  unknown (no reject), B's worker-side check is additive (a worker without it
  just hits the old 127 path — no regression).

## Deploy (flagged, separate step — multi-host)

Broker (A + the refuse-admission/next-job/schema change) runs on **gt76**
(docker). Worker change (B's cwd check) runs on **all four** workers (laptop,
desktop, gt76, msi-4080), each with its own checkout. Editable install on the
laptop (`/home/mjarnold/jobd/src`) picks up `src/` changes on
`job-worker.service` restart; other hosts need their checkout updated + worker
restart; the broker container needs a rebuild/restart. Schema column must land
on the broker's DB. Sequence + per-host recipe → plan / a deploy checklist. Code

- tests are the primary deliverable here; deploy is gated on user go-ahead.
