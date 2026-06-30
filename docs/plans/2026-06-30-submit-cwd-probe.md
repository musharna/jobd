# Submit-time cwd probe + worker-side cwd re-queue — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the silent-misroute class — a host-local `cwd` (e.g. a git worktree under `/home`, which every worker advertises) routing to a host that lacks it and dying `exit 127`. Add (A) a submit-time mount_roots prefix probe and (B) a worker-side `os.path.isdir(cwd)` check that re-queues via refuse-admission with a per-job worker-exclusion and a `cwd_unreachable` terminal.

**Architecture:** Broker (FastAPI + SQLAlchemy, `src/jobd/app.py`, `matcher.py`, `models.py`) + pull-based worker (`src/jobd/worker/job_worker.py`). A threads `mount_roots` onto `WorkerSnapshot` and adds a pure `cwd_routability()` checked at `/submit`. B adds an `excluded_workers_json` column to `Job`, extends `/jobs/{id}/refuse-admission` with a `cwd_missing` branch (record exclusion + terminal when no eligible worker remains), filters excluded workers at `/next-job`, and adds the worker-side cwd check. All changes are additive/back-compat.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy (StrEnum `JobState`), Pydantic v2 models, httpx (worker↔broker), pytest. Spec: `docs/plans/2026-06-30-submit-cwd-probe-design.md`.

## Global Constraints

- **Base branch:** `feat/submit-cwd-probe` off `public` (v0.5.5) — the deployed line. Worktree: `/home/mjarnold/jobd-worktrees/cwd-probe`.
- **Test invocation (run from the worktree):** `cd /home/mjarnold/jobd-worktrees/cwd-probe && PYTHONPATH=src /home/mjarnold/jobd/.venv/bin/python -m pytest <args>`.
- **Back-compat is mandatory.** New model fields default to current behavior (`AdmissionRefusal.reason="gpu_contention"`, `cwd=None`; `WorkerSnapshot.mount_roots=[]`; `Job.excluded_workers_json` NULL→`[]`). Existing tests must stay green. Old workers (empty mount_roots, no cwd-check) must keep working.
- **A never false-rejects on missing data:** a worker with empty `mount_roots` is "unknown" and is excluded from any deny decision.
- **No hot loop:** each worker refuses a given job at most once (then excluded); when all eligible workers are excluded the job goes terminal `cwd_unreachable`, never stuck QUEUED.
- **Follow existing patterns:** mirror the `*_json` nullable-TEXT column style (`Worker.mount_roots_json`, `host_aliases_json`, `tags_json`) and the existing refuse-admission call site (`job_worker.py:1416`).
- **Commit footer (every commit):**
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01WN7txqrW259ksUC4t5WiCU
  ```
- **Deploy is out of scope for these tasks** (multi-host; gated on user go-ahead). Task 8 writes the deploy checklist only.

---

### Task 1: `WorkerSnapshot.mount_roots` field + `_build_snapshots` population

**Files:**

- Modify: `src/jobd/matcher.py` (`WorkerSnapshot` dataclass, ~line 25)
- Modify: `src/jobd/app.py` (`_build_snapshots`, the function shown below)
- Test: `tests/test_matcher.py`

**Interfaces:**

- Produces: `WorkerSnapshot.mount_roots: list[str]` (default `[]`).

- [ ] **Step 1: Write the failing test** — append to `tests/test_matcher.py`:

```python
def test_worker_snapshot_carries_mount_roots():
    from jobd.matcher import WorkerSnapshot
    w = WorkerSnapshot(
        host="laptop", host_aliases=["any"], free_vram_gb=20.0,
        unregistered_vram_gb=0.0, free_ram_gb=16.0, idle_cpus=8,
        mount_roots=["/home", "/tmp"],
    )
    assert w.mount_roots == ["/home", "/tmp"]

def test_worker_snapshot_mount_roots_defaults_empty():
    from jobd.matcher import WorkerSnapshot
    w = WorkerSnapshot(
        host="x", host_aliases=[], free_vram_gb=0.0, unregistered_vram_gb=0.0,
        free_ram_gb=8.0, idle_cpus=4,
    )
    assert w.mount_roots == []
```

- [ ] **Step 2: Run, expect fail**

Run: `PYTHONPATH=src /home/mjarnold/jobd/.venv/bin/python -m pytest tests/test_matcher.py -k mount_roots -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'mount_roots'`.

- [ ] **Step 3: Add the field** — in `src/jobd/matcher.py`, in the `WorkerSnapshot` dataclass after `tags: list[str] = field(default_factory=list)`:

```python
    mount_roots: list[str] = field(default_factory=list)
```

- [ ] **Step 4: Populate in `_build_snapshots`** — in `src/jobd/app.py`, add one line inside the `WorkerSnapshot(...)` constructor (after `tags=json.loads(w.tags_json or "[]"),`):

```python
            mount_roots=json.loads(w.mount_roots_json or "[]"),
```

- [ ] **Step 5: Run, expect pass + no regressions**

Run: `PYTHONPATH=src /home/mjarnold/jobd/.venv/bin/python -m pytest tests/test_matcher.py -q`
Expected: PASS (44 + 2 new).

- [ ] **Step 6: Commit**

```bash
git add src/jobd/matcher.py src/jobd/app.py tests/test_matcher.py
git commit -F - <<'EOF'
feat(matcher): WorkerSnapshot.mount_roots + _build_snapshots population

Threads the worker-reported mount_roots prefixes onto the snapshot so
submit-time routability checks can read them. Default [] = back-compat.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01WN7txqrW259ksUC4t5WiCU
EOF
```

---

### Task 2: `cwd_routability()` pure function (A core logic)

**Files:**

- Modify: `src/jobd/matcher.py` (new function beside `submit_preflight`, ~line 274)
- Test: `tests/test_matcher.py`

**Interfaces:**

- Consumes: `WorkerSnapshot.mount_roots` (Task 1).
- Produces:

  ```python
  def cwd_routability(
      cwd: str, host_pin: str, all_known_workers: list[WorkerSnapshot]
  ) -> tuple[bool, str] | None
  ```

  Returns `None` when routable/unknown; `(True, msg)` = hard-deny (caller 400s); `(False, msg)` = soft-warning (caller folds into warnings).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_matcher.py`:

```python
def _w(host, roots, aliases=None):
    from jobd.matcher import WorkerSnapshot
    return WorkerSnapshot(
        host=host, host_aliases=aliases or ["any"], free_vram_gb=10.0,
        unregistered_vram_gb=0.0, free_ram_gb=16.0, idle_cpus=8,
        mount_roots=roots,
    )

def test_cwd_routability_pinned_host_no_cover_hard_deny():
    from jobd.matcher import cwd_routability
    workers = [_w("desktop", ["/home", "/tmp"])]
    out = cwd_routability("/mnt/d/data/x", "desktop", workers)
    assert out is not None and out[0] is True
    assert "/mnt/d/data/x" in out[1]

def test_cwd_routability_any_pin_no_cover_soft_warning():
    from jobd.matcher import cwd_routability
    workers = [_w("gt76", ["/home", "/tmp"]), _w("msi", ["/home"])]
    out = cwd_routability("/scratch/run1", "any", workers)
    assert out is not None and out[0] is False
    assert "/scratch/run1" in out[1]

def test_cwd_routability_covered_returns_none():
    from jobd.matcher import cwd_routability
    workers = [_w("laptop", ["/home", "/mnt/c"])]
    assert cwd_routability("/home/u/proj", "any", workers) is None

def test_cwd_routability_empty_mount_roots_is_unknown_no_reject():
    from jobd.matcher import cwd_routability
    # worker advertises nothing (old worker) -> unknown, never reject
    workers = [_w("legacy", [])]
    assert cwd_routability("/anything/at/all", "any", workers) is None
    assert cwd_routability("/anything/at/all", "legacy", workers) is None

def test_cwd_routability_worktree_under_home_NOT_caught_by_A():
    # Documents the A/B split: every worker advertises /home, so the prefix
    # probe passes a worktree cwd. B (worker-side isdir) is what catches it.
    from jobd.matcher import cwd_routability
    workers = [_w("laptop", ["/home"]), _w("desktop", ["/home"])]
    cwd = "/home/u/proj/.claude/worktrees/wt"
    assert cwd_routability(cwd, "any", workers) is None
```

- [ ] **Step 2: Run, expect fail** (`ImportError: cannot import name 'cwd_routability'`)

Run: `PYTHONPATH=src /home/mjarnold/jobd/.venv/bin/python -m pytest tests/test_matcher.py -k cwd_routability -q`

- [ ] **Step 3: Implement** — add to `src/jobd/matcher.py` after `submit_preflight` (it ends ~line 274):

```python
def _covers(w: WorkerSnapshot, cwd: str) -> bool:
    """True iff the worker advertises a mount_root that prefixes cwd.

    Empty mount_roots = the worker doesn't advertise (old worker) = unknown;
    treated as 'can't assert', never as 'can't cover'.
    """
    return bool(w.mount_roots) and any(cwd.startswith(r) for r in w.mount_roots)


def cwd_routability(
    cwd: str, host_pin: str, all_known_workers: list[WorkerSnapshot]
) -> tuple[bool, str] | None:
    """Submit-time mount_roots reachability check for `cwd`.

    Returns None when routable or undecidable; (True, msg) for a hard deny
    (the caller should 400); (False, msg) for a soft warning (fold into the
    submit warnings). Generalizes the /mnt/c guard: it covers any host-local
    prefix a worker fails to advertise.

    Workers with empty mount_roots are 'unknown' and excluded from the deny
    decision, so stale/old workers never cause a false reject. A cwd under a
    prefix EVERY worker advertises (e.g. /home) is considered routable here —
    the worker-side os.path.isdir check (B) is the layer that catches a
    host-local path under a shared prefix (e.g. a git worktree).
    """
    # Workers that actually advertise something (can participate in the decision).
    advertising = [w for w in all_known_workers if w.mount_roots]
    if not advertising:
        return None  # nobody advertises roots -> can't assert anything

    if host_pin != "any":
        pinned = [
            w for w in advertising if host_pin in (w.host, *w.host_aliases)
        ]
        if not pinned:
            return None  # pinned host doesn't advertise roots (or unknown) -> defer
        if any(_covers(w, cwd) for w in pinned):
            return None
        roots = sorted({r for w in pinned for r in w.mount_roots})
        return (
            True,
            f"cwd {cwd!r} is under no mount_root of host_pin={host_pin!r} "
            f"(roots: {roots}). Pass --host <a-host-that-has-it>, or stage the "
            f"data under a path that host advertises.",
        )

    # host_pin == "any": warn only if NO advertising worker covers it.
    if any(_covers(w, cwd) for w in advertising):
        return None
    return (
        False,
        f"cwd {cwd!r} is under no known worker's mount_roots; it may sit queued "
        f"or fail to route. Pass --host <the-host-that-has-it>, or stage under a "
        f"shared path (e.g. /tmp).",
    )
```

- [ ] **Step 4: Run, expect pass**

Run: `PYTHONPATH=src /home/mjarnold/jobd/.venv/bin/python -m pytest tests/test_matcher.py -q`
Expected: PASS (all, incl. 5 new).

- [ ] **Step 5: Commit**

```bash
git add src/jobd/matcher.py tests/test_matcher.py
git commit -F - <<'EOF'
feat(matcher): cwd_routability() — submit-time mount_roots reachability probe

Generalizes the /mnt/c guard: hard-deny when a pinned host advertises no
mount_root covering cwd; soft-warn when host_pin=any and no worker covers it.
Empty mount_roots = unknown (never false-reject). A shared-prefix path (/home)
is routable here by design — the worker-side isdir check catches host-local
paths under it.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01WN7txqrW259ksUC4t5WiCU
EOF
```

---

### Task 3: Wire `cwd_routability` into `/submit` (A integration)

**Files:**

- Modify: `src/jobd/app.py` (`submit()`, after the `/mnt/c` block ~line 427 and near the `submit_preflight` call ~line 447)
- Test: `tests/test_api.py`

**Interfaces:**

- Consumes: `cwd_routability` (Task 2), the existing `all_snapshots` (`app.py:444`) and `warnings` list (`app.py:448`).

- [ ] **Step 1: Write the failing tests** — mirror `test_submit_rejects_mnt_c_cwd_for_non_laptop` (`tests/test_api.py:1413`). Find that test and the fixtures it uses (a `client` fixture + a way to register workers with mount_roots — read how the file registers a worker; reuse the same helper). Add:

```python
def test_submit_hard_denies_pinned_cwd_without_mount_root(client):
    # Register a worker whose mount_roots do NOT cover the cwd, pinned to it.
    _register_worker(client, host="desktop", mount_roots=["/home", "/tmp"])
    r = client.post("/submit", json={
        "cmd": ["true"], "cwd": "/mnt/d/data/x",
        "project": "project-a", "host_pin": "desktop",
    })
    assert r.status_code == 400
    assert "/mnt/d/data/x" in r.text

def test_submit_warns_any_pin_uncovered_cwd_but_accepts(client):
    _register_worker(client, host="gt76", mount_roots=["/home", "/tmp"])
    r = client.post("/submit", json={
        "cmd": ["true"], "cwd": "/scratch/run1",
        "project": "project-a", "host_pin": "any",
    })
    assert r.status_code == 200
    body = r.json()
    assert body.get("warning") and "/scratch/run1" in body["warning"]
```

(Read the file's existing worker-registration test helper — likely a `/heartbeat` POST. If none exists as a named helper, inline a `client.post("/heartbeat", json={...})` with `mount_roots` set, matching the `WorkerHeartbeat` schema at `models.py:256`. Name your helper `_register_worker` and define it once.)

- [ ] **Step 2: Run, expect fail** (no 400 / no warning yet)

Run: `PYTHONPATH=src /home/mjarnold/jobd/.venv/bin/python -m pytest tests/test_api.py -k "cwd" -q`

- [ ] **Step 3: Implement** — in `src/jobd/app.py submit()`, right after the existing `preflight_warn = submit_preflight(...)` line (`~:447`), before the `warnings = [...]` list is built:

```python
            cwd_route = cwd_routability(req.cwd, host_pin, all_snapshots)
            cwd_route_warn: str | None = None
            if cwd_route is not None:
                is_hard, cwd_msg = cwd_route
                if is_hard:
                    raise HTTPException(status_code=400, detail=cwd_msg)
                cwd_route_warn = cwd_msg
```

Then add `cwd_route_warn` to the `warnings` tuple comprehension (`~:448`):

```python
            warnings = [
                w
                for w in (unknown_project_warning, preflight_warn, cwd_route_warn, ser_warn, gpu_warn)
                if w is not None
            ]
```

Add the import at the top of `app.py` where `submit_preflight` is imported (search `submit_preflight` in the import block) — add `cwd_routability` to that import.

- [ ] **Step 4: Run, expect pass + full suite green**

Run: `PYTHONPATH=src /home/mjarnold/jobd/.venv/bin/python -m pytest tests/test_api.py -q`
Expected: PASS incl. the 2 new; the existing `/mnt/c` test still passes (untouched fast-path).

- [ ] **Step 5: Commit**

```bash
git add src/jobd/app.py tests/test_api.py
git commit -F - <<'EOF'
feat(submit): wire cwd_routability — 400 on pinned-unreachable, warn on any-pin

Hard-400s a pinned cwd no mount_root covers (generalizes /mnt/c); folds the
any-pin "no worker covers cwd" case into the submit warnings. /mnt/c fast-path
unchanged.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01WN7txqrW259ksUC4t5WiCU
EOF
```

---

### Task 4: `Job.excluded_workers_json` column + read helper (B schema)

**Files:**

- Modify: `src/jobd/models.py` (`Job` ORM — mirror the `mount_roots_json` nullable-TEXT pattern)
- Modify: `src/jobd/app.py` (a `_excluded_workers(job) -> list[str]` helper near `_build_snapshots`)
- Test: `tests/test_api.py`

**Interfaces:**

- Produces: `Job.excluded_workers_json: Mapped[str | None]` (nullable TEXT, JSON list); `_excluded_workers(job) -> list[str]` (NULL→`[]`).

- [ ] **Step 1: Write the failing test** — append to `tests/test_api.py`:

```python
def test_excluded_workers_defaults_empty(client):
    # A freshly submitted job has no excluded workers.
    _register_worker(client, host="laptop", mount_roots=["/home"])
    r = client.post("/submit", json={
        "cmd": ["true"], "cwd": "/home/u/p", "project": "project-a",
    })
    jid = r.json()["id"]
    # read it back via the broker's own helper through a status route
    info = client.get(f"/jobs/{jid}").json()
    # excluded_workers not surfaced in JobInfo; assert the DB default via a
    # direct broker call is covered in Task 5. Here assert submit succeeded.
    assert r.status_code == 200 and jid > 0
```

(This is a thin guard; the substantive exclusion behavior is tested in Tasks 5-6. Confirm the column exists by importing the ORM and checking the attribute.)

```python
def test_job_orm_has_excluded_workers_column():
    from jobd.models import Job
    assert hasattr(Job, "excluded_workers_json")
```

- [ ] **Step 2: Run, expect fail** (`AttributeError` on the ORM attr)

Run: `PYTHONPATH=src /home/mjarnold/jobd/.venv/bin/python -m pytest tests/test_api.py -k excluded -q`

- [ ] **Step 3: Add the column** — in `src/jobd/models.py`, in the `Job` ORM class, mirror an existing nullable-TEXT JSON column (find `requires_json` or another `Mapped[str | None] = mapped_column(...)` in `Job`) and add:

```python
    excluded_workers_json: Mapped[str | None] = mapped_column(default=None)
```

- [ ] **Step 4: Add the read helper** — in `src/jobd/app.py`, near `_build_snapshots`:

```python
def _excluded_workers(job: Job) -> list[str]:
    """Hosts that refused this job for a missing cwd (back-compat: NULL -> [])."""
    return json.loads(job.excluded_workers_json or "[]")
```

- [ ] **Step 5: Confirm schema init path** — read how the broker creates tables (search `create_all` in `app.py`/`db.py`/`models.py`). If it's `Base.metadata.create_all`, the additive nullable column appears on fresh DBs automatically; for the **live** broker DB, note in the deploy checklist (Task 8) that the column must be added (`ALTER TABLE job ADD COLUMN excluded_workers_json TEXT`) since `create_all` does not alter existing tables. If an Alembic/migration dir exists, add a migration instead. Record what you found in the commit body.

- [ ] **Step 6: Run, expect pass + suite green**

Run: `PYTHONPATH=src /home/mjarnold/jobd/.venv/bin/python -m pytest tests/test_api.py -q`

- [ ] **Step 7: Commit**

```bash
git add src/jobd/models.py src/jobd/app.py tests/test_api.py
git commit -F - <<'EOF'
feat(models): Job.excluded_workers_json + _excluded_workers helper

Per-job set of hosts that refused the job for a missing cwd, so the matcher
won't re-offer it to them. Nullable TEXT, NULL -> [] (back-compat). [Schema
init path noted for deploy: create_all vs ALTER.]

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01WN7txqrW259ksUC4t5WiCU
EOF
```

---

### Task 5: `refuse-admission` `cwd_missing` branch + `cwd_unreachable` terminal (B broker)

**Files:**

- Modify: `src/jobd/models.py` (`AdmissionRefusal` — add `reason`, `cwd`)
- Modify: `src/jobd/app.py` (`refuse_admission` handler, `:913`)
- Test: `tests/test_api.py`

**Interfaces:**

- Consumes: `_excluded_workers` (Task 4), `eligible_workers` (`matcher.py:199`), `_build_snapshots`.
- Produces: refuse-admission with `reason="cwd_missing"` records the worker in `excluded_workers_json`; when eligible-minus-excluded is empty, the job goes `FAILED` / `termination_reason="cwd_unreachable"`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_api.py`. Use the existing pattern for driving a job to ASSIGNED on a worker (find how other refuse-admission/next-job tests do it — likely submit → `/next-job` poll as the worker → job ASSIGNED). Then:

```python
def test_refuse_admission_cwd_missing_records_exclusion_and_requeues(client):
    # Two workers both advertise /home; job cwd is a host-local worktree.
    _register_worker(client, host="laptop", mount_roots=["/home"])
    _register_worker(client, host="desktop", mount_roots=["/home"])
    jid = _submit_and_assign(client, cwd="/home/u/p/.claude/worktrees/wt",
                             worker="desktop")
    r = client.post(f"/jobs/{jid}/refuse-admission",
                    json={"reason": "cwd_missing", "cwd": "/home/u/p/.claude/worktrees/wt"})
    assert r.status_code == 200
    info = r.json()
    assert info["state"] == "queued"           # re-queued, not failed (laptop remains)
    # desktop is now excluded: a /next-job poll as desktop must not get it
    got = client.post("/next-job", json={"host": "desktop", "mount_roots": ["/home"]}).json()
    assert got is None or got["id"] != jid
    # laptop still gets it
    got2 = client.post("/next-job", json={"host": "laptop", "mount_roots": ["/home"]}).json()
    assert got2 is not None and got2["id"] == jid

def test_refuse_admission_cwd_missing_terminal_when_no_eligible_left(client):
    _register_worker(client, host="desktop", mount_roots=["/home"])
    jid = _submit_and_assign(client, cwd="/home/u/p/wt", worker="desktop")
    r = client.post(f"/jobs/{jid}/refuse-admission",
                    json={"reason": "cwd_missing", "cwd": "/home/u/p/wt"})
    assert r.status_code == 200
    info = r.json()
    assert info["state"] == "failed"
    assert info["termination_reason"] == "cwd_unreachable"

def test_refuse_admission_gpu_contention_unchanged(client):
    # Existing behavior: default reason, no cwd, no exclusion, back to queued.
    _register_worker(client, host="desktop", mount_roots=["/home"])
    jid = _submit_and_assign(client, cwd="/home/u/p", worker="desktop")
    r = client.post(f"/jobs/{jid}/refuse-admission",
                    json={"required_gb": 20.0, "free_gb": 2.0})
    assert r.status_code == 200
    assert r.json()["state"] == "queued"
```

(Define `_submit_and_assign(client, cwd, worker)` once: submit a job, then POST `/next-job` as `worker` to claim it, return the job id. Mirror existing next-job test usage in the file.)

- [ ] **Step 2: Run, expect fail**

Run: `PYTHONPATH=src /home/mjarnold/jobd/.venv/bin/python -m pytest tests/test_api.py -k "refuse_admission" -q`

- [ ] **Step 3: Extend the model** — in `src/jobd/models.py`, `AdmissionRefusal` (`:301`): make the GPU fields optional and add the reason/cwd. Read the current fields first; change to:

```python
class AdmissionRefusal(BaseModel):
    reason: str = "gpu_contention"
    cwd: str | None = None
    required_gb: float | None = None
    free_gb: float | None = None
    foreign_pids: list[int] = Field(default_factory=list)
    foreign_vram_gb: float = 0.0
```

(Keep every existing field name; only add `reason`/`cwd` and relax `required_gb`/`free_gb` to optional with defaults so the cwd path can omit them. Verify the existing GPU-contention caller still validates.)

- [ ] **Step 4: Branch the handler** — in `src/jobd/app.py refuse_admission` (`:913`), after the `state != ASSIGNED` 409 guard and the `prior_worker = job.worker` line, split on reason:

```python
            prior_worker = job.worker
            if payload.reason == "cwd_missing":
                excluded = _excluded_workers(job)
                if prior_worker and prior_worker not in excluded:
                    excluded.append(prior_worker)
                job.excluded_workers_json = json.dumps(excluded)
                # Re-derive eligibility minus the exclusion set.
                online = list(
                    session.execute(select(Worker).where(Worker.state == "online")).scalars().all()
                )
                snaps = _build_snapshots(online)
                requires = (
                    JobRequires.model_validate_json(job.requires_json)
                    if job.requires_json and job.requires_json != "{}"
                    else None
                )
                elig = [
                    w for w in eligible_workers(requires, job.host_pin, snaps)
                    if w.host not in excluded
                ]
                _emit_event(
                    logs_dir, "cwd_refused", source="broker", job_id=job_id,
                    project=job.project, worker=prior_worker, cwd=payload.cwd,
                )
                if not elig:
                    job.state = JobState.FAILED
                    job.worker = None
                    job.termination_reason = "cwd_unreachable"
                    session.commit()
                    # Surface a log line so `job logs` explains the failure.
                    _append_job_log(
                        logs_dir, job_id,
                        f"[broker] cwd {payload.cwd!r} exists on no eligible worker "
                        f"(excluded: {excluded}); failing cwd_unreachable\n",
                    )
                    session.refresh(job)
                    return _to_info(job)
                job.state = JobState.QUEUED
                job.worker = None
                job.started_at = None
                session.commit()
                _wake_dispatchers()
                session.refresh(job)
                return _to_info(job)
            # --- default: gpu_contention (unchanged) ---
            job.state = JobState.QUEUED
            job.worker = None
            job.started_at = None
            session.commit()
            _emit_event(
                logs_dir, "admission_blocked", source="broker", job_id=job_id,
                project=job.project, worker=prior_worker,
                required_gb=payload.required_gb, free_gb=payload.free_gb,
                foreign_pids=list(payload.foreign_pids),
                foreign_vram_gb=payload.foreign_vram_gb,
            )
            _wake_dispatchers()
            session.refresh(job)
            return _to_info(job)
```

Verify the imports `eligible_workers`, `JobRequires`, `select`, `Worker`, `JobState` are already in `app.py` (they are used elsewhere in the file). For the log line, reuse the existing job-log append mechanism — find how `/jobs/{id}/log` writes (the worker POSTs to it; locate the broker-side writer, e.g. `_append_job_log` or the log file path helper) and call that; if there is no in-process helper, write to the same log path the `/jobs/{id}/log` route uses. If `required_gb`/`free_gb` are now optional, guard the `admission_blocked` event emit against `None` (it already accepts them).

- [ ] **Step 5: Run, expect pass + suite green**

Run: `PYTHONPATH=src /home/mjarnold/jobd/.venv/bin/python -m pytest tests/test_api.py -q`
Expected: PASS incl. 3 new; the existing GPU-contention refuse-admission test still green.

- [ ] **Step 6: Commit**

```bash
git add src/jobd/models.py src/jobd/app.py tests/test_api.py
git commit -F - <<'EOF'
feat(broker): refuse-admission cwd_missing branch — exclude + requeue/terminal

A worker that finds cwd missing refuses with reason=cwd_missing; broker records
the host in excluded_workers_json and re-queues, or fails the job
cwd_unreachable when no eligible worker remains. gpu_contention path unchanged.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01WN7txqrW259ksUC4t5WiCU
EOF
```

---

### Task 6: `/next-job` exclusion filter (B routing)

**Files:**

- Modify: `src/jobd/app.py` (`/next-job` handler, beside the mount_roots filter `:1172`)
- Test: covered by Task 5's `test_refuse_admission_cwd_missing_records_exclusion_and_requeues` (the re-offer assertions). Add one focused unit too.

**Interfaces:**

- Consumes: `_excluded_workers` (Task 4), `q.host` (the polling worker).

- [ ] **Step 1: Write the failing test** — append to `tests/test_api.py`:

```python
def test_next_job_skips_excluded_worker(client):
    _register_worker(client, host="laptop", mount_roots=["/home"])
    _register_worker(client, host="desktop", mount_roots=["/home"])
    jid = _submit_and_assign(client, cwd="/home/u/p/wt", worker="desktop")
    client.post(f"/jobs/{jid}/refuse-admission",
                json={"reason": "cwd_missing", "cwd": "/home/u/p/wt"})
    # desktop excluded -> never re-offered
    got = client.post("/next-job", json={"host": "desktop", "mount_roots": ["/home"]}).json()
    assert got is None or got["id"] != jid
```

- [ ] **Step 2: Run, expect fail** (desktop still gets re-offered the job)

Run: `PYTHONPATH=src /home/mjarnold/jobd/.venv/bin/python -m pytest tests/test_api.py -k next_job_skips_excluded -q`

- [ ] **Step 3: Implement** — in `src/jobd/app.py`, in the `/next-job` handler right after the existing mount_roots filter (`:1172`):

```python
                # Drop jobs this host has refused for a missing cwd (B): the
                # exclusion set guarantees a refused job re-routes elsewhere and
                # never hot-loops back to the same host.
                queued = [j for j in queued if q.host not in _excluded_workers(j)]
```

- [ ] **Step 4: Run, expect pass + suite green**

Run: `PYTHONPATH=src /home/mjarnold/jobd/.venv/bin/python -m pytest tests/test_api.py -q`

- [ ] **Step 5: Commit**

```bash
git add src/jobd/app.py tests/test_api.py
git commit -F - <<'EOF'
feat(broker): /next-job skips workers in a job's cwd-exclusion set

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01WN7txqrW259ksUC4t5WiCU
EOF
```

---

### Task 7: Worker-side `os.path.isdir(cwd)` check → refuse-admission (B worker)

**Files:**

- Modify: `src/jobd/worker/job_worker.py` (`run_job`, before the launcher check `:819`)
- Test: `tests/` (worker test — find the existing worker test module; mirror its httpx-client mocking)

**Interfaces:**

- Consumes: the broker `/jobs/{id}/refuse-admission` `cwd_missing` contract (Task 5).

- [ ] **Step 1: Write the failing test** — find the worker test file (search `tests/` for `run_job` or `job_worker`). Mirror its style (a fake/mock httpx client capturing POSTs). Add a test: calling `run_job` with a `cwd` that doesn't exist POSTs to `/jobs/{id}/refuse-admission` with `reason="cwd_missing"` and does NOT start a subprocess / does NOT POST `/complete` with exit 127.

```python
def test_run_job_refuses_admission_when_cwd_missing(monkeypatch):
    from jobd.worker import job_worker
    posts = []
    class FakeClient:
        def post(self, path, **kw):
            posts.append((path, kw))
            class R:  # minimal response
                status_code = 200
                def json(self_inner): return {}
            return R()
    job = {"id": 7, "cmd": ["true"], "cwd": "/no/such/dir/xyz", "env": {}}
    job_worker.run_job(FakeClient(), job, set())
    paths = [p for p, _ in posts]
    assert any(p == "/jobs/7/refuse-admission" for p in paths)
    body = next(kw for p, kw in posts if p == "/jobs/7/refuse-admission")
    assert body["json"]["reason"] == "cwd_missing"
    assert not any(p == "/jobs/7/complete" for p in paths)  # did not exit 127
```

(Adjust to the real `run_job` signature and the test file's existing mock conventions — read one existing worker test first. If `run_job` checks `_drain_event` first, ensure the test doesn't trip the drain path.)

- [ ] **Step 2: Run, expect fail**

Run: `PYTHONPATH=src /home/mjarnold/jobd/.venv/bin/python -m pytest tests/ -k "cwd_missing and run_job" -q`

- [ ] **Step 3: Implement** — in `src/jobd/worker/job_worker.py run_job`, immediately before the launcher check (`missing = _missing_launcher_path(cmd, cwd)`, `:819`), after the env setup:

```python
    # Host-local cwd absent on this worker (e.g. a git worktree that lives only
    # on another host). Refuse admission so the broker re-routes to a host that
    # has it — instead of cd-failing to exit 127. The broker excludes this host
    # for this job, so it won't be re-offered here (no hot loop).
    if not os.path.isdir(cwd):
        print(f"[worker] job {job_id}: cwd missing here ({cwd}); refusing admission",
              file=sys.stderr)
        try:
            client.post(
                f"/jobs/{job_id}/refuse-admission",
                json={"reason": "cwd_missing", "cwd": cwd},
                timeout=10.0,
            )
        except Exception as e:
            print(f"[worker] cwd-missing refuse post failed for job {job_id}: {e}",
                  file=sys.stderr)
        return
```

(`os` and `sys` are already imported in this module — `os.path.isdir` is used at `:616`.)

- [ ] **Step 4: Run, expect pass + worker suite green**

Run: `PYTHONPATH=src /home/mjarnold/jobd/.venv/bin/python -m pytest tests/ -q`

- [ ] **Step 5: Commit**

```bash
git add src/jobd/worker/job_worker.py tests/
git commit -F - <<'EOF'
feat(worker): refuse admission when cwd is missing (re-route, not exit 127)

Before the launcher check, verify os.path.isdir(cwd). If absent, POST
refuse-admission reason=cwd_missing so the broker re-routes to a host that has
the path, instead of cd-failing to exit 127.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01WN7txqrW259ksUC4t5WiCU
EOF
```

---

### Task 8: Full suite + real-execution check + bank + deploy checklist

**Files:**

- Modify: `CHANGELOG.md` (if present; else `docs/`)
- Create: `docs/plans/2026-06-30-submit-cwd-probe-deploy.md` (multi-host deploy checklist)

- [ ] **Step 1: Full suite green + lint**

```bash
cd /home/mjarnold/jobd-worktrees/cwd-probe
PYTHONPATH=src /home/mjarnold/jobd/.venv/bin/python -m pytest tests/ -q
/home/mjarnold/jobd/.venv/bin/python -m ruff check src/ tests/   # if ruff configured (pyproject [tool.ruff])
```

Expected: all green; ruff clean (the repo configures ruff at `pyproject.toml:86`).

- [ ] **Step 2: Real-execution check (documented, NOT auto-deployed).** Write the live walkthrough steps into the deploy doc: after deploy, submit the exact worktree cwd with `--gpu` (no `--host`) and confirm it re-routes to `laptop` and runs (or fails `cwd_unreachable` with the hint) instead of the old silent 127. This is the multi-host boundary the synthetic tests can't cover.

- [ ] **Step 3: Write the deploy checklist** `docs/plans/2026-06-30-submit-cwd-probe-deploy.md`: the schema column add on the live broker DB (from Task 4 Step 5 finding: `create_all` won't alter an existing table → `ALTER TABLE job ADD COLUMN excluded_workers_json TEXT` on the broker DB, or a migration); broker rebuild/restart on gt76 (docker); worker update + restart on all four hosts (laptop editable install picks up `src/` on `job-worker.service` restart; desktop/gt76/msi need their checkout updated). Order: schema → broker → workers. Include the rollback (revert + restart; the column is harmless if unused).

- [ ] **Step 4: CHANGELOG / bank** — prepend a changelog entry (read `CHANGELOG.md` first if it exists; match its style): the silent-misroute fix, A (submit prefix probe) + B (worker cwd re-queue + exclusion + terminal), back-compat, the A/B split rationale, the live-incident origin (2026-06-30 jepagame worktree job).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -F - <<'EOF'
docs(routing): cwd-probe deploy checklist + changelog; full suite green

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01WN7txqrW259ksUC4t5WiCU
EOF
```

---

## Self-Review

**1. Spec coverage:** A snapshot field (T1) → routability fn with the 5 cases incl. the worktree A/B-split documentation (T2) → submit wiring hard-400/warn (T3). B column+helper (T4) → refuse-admission cwd_missing branch + exclusion + cwd_unreachable terminal (T5) → next-job exclusion filter (T6) → worker isdir check (T7). Real-exec + deploy + bank (T8). Every spec section maps to a task. ✓

**2. Placeholder scan:** The deferred specifics are all "read the live helper and mirror it" instructions for established patterns the engineer must match (worker-registration test helper, `_append_job_log` writer, the worker test mock, the schema-init path) — not uncoded gated deliverables. Each has a concrete fallback. No TBDs in load-bearing logic. The `cwd_routability`, `refuse_admission` branch, `/next-job` filter, and worker check are all shown in full.

**3. Type consistency:** `cwd_routability -> tuple[bool, str] | None` consistent across T2 def, T3 use. `_excluded_workers(job) -> list[str]` consistent T4 def / T5,T6 use. `AdmissionRefusal.reason/cwd` consistent T5 model / T7 worker payload. `WorkerSnapshot.mount_roots` consistent T1 def / T2 use. `excluded_workers_json` column name consistent T4/T5/T6.

**Open risks (flagged, not gaps):** (a) the test-helper names (`_register_worker`, `_submit_and_assign`) must be reconciled with whatever `tests/test_api.py` already provides — the plan says define-once/mirror-existing; (b) the live-DB schema add is a real deploy step, isolated to T8's checklist; (c) `AdmissionRefusal` relaxing `required_gb`/`free_gb` to optional must not break the existing GPU caller — T5 Step 3 calls for verifying that, and `test_refuse_admission_gpu_contention_unchanged` guards it.
