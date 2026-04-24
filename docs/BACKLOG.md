# jobd Backlog

Feature requests and known issues. File one entry per item, newest on top. Move
closed items to the bottom under `## Done`.

## Open

### --depends-on flag for job chaining

**Filed:** 2026-04-23
**Priority:** medium
**Requested by:** orchid-sdxl (post-v7 eval chain)

Add CLI + broker support for one-way dependency edges:

```
job submit --project X --depends-on 1234 --depends-on 1235 -- cmd...
```

Semantics:

- Broker stores `depends_on: list[int]` on the job row.
- Matcher refuses to dispatch a job while any parent is in a non-terminal state
  (queued, running, etc.).
- Parent reaching `completed` → child becomes eligible for dispatch.
- Parent reaching `failed` / `cancelled` → child is marked `cancelled` with
  reason `parent_failed` (configurable later; conservative default is
  cancel-on-parent-failure). Leave a `--depends-on-any-exit` escape hatch for
  "run regardless of parent outcome".
- `job list` should show `deps: 1234✓ 1235⏳` or similar so the queue state is
  visible.
- `job wait <child>` should block on the full chain (already mostly free once
  the child is tracked).

Implementation sketch:

- DB migration: `ALTER TABLE jobs ADD COLUMN depends_on TEXT` (JSON list of
  parent ids).
- Matcher: extend eligibility query with `AND NOT EXISTS (SELECT 1 FROM jobs p
WHERE p.id IN (...) AND p.state NOT IN ('completed','failed','cancelled'))`.
- Client: add repeatable `--depends-on INT` to `job submit` in
  `src/cli.py`.
- Tests: chain of 3 (A → B → C), fan-in (C depends on A+B), parent failure
  cancels child, parent cancel cascades.

Non-goals:

- No DAG/fan-out scheduler. Just one-way edges; user composes graphs.
- No retry-on-parent-retry. If a parent is rerun, it is a new job id; user
  resubmits the child against the new id.

Unblocks: letting Claude Code sessions queue eval / post-processing jobs behind
a long training job without a PID-wait wrapper.

## Done
