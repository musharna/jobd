# Agent cookbook — driving jobd from Claude Code (or any MCP agent)

jobd ships a first-class MCP server, so an LLM agent can treat your GPU fleet
the way it treats any other tool: submit work, check on it, react to
preemption, and clean up — without shelling out or SSHing anywhere. This page
is a worked tour of the patterns that make that reliable.

## Setup

```bash
# one-time: register the MCP server with Claude Code
claude mcp add jobd -e JOBD_URL=http://<broker-tailscale-ip>:8765 \
                    -e JOBD_API_TOKEN=<token> -- jobd-mcp
```

The server exposes nine tools: `jobd_submit`, `jobd_status`, `jobd_logs`,
`jobd_list`, `jobd_cancel`, `jobd_preempt`, `jobd_workers`,
`jobd_worker_delete`, `jobd_events`. Submitted `env` values are masked on
every read surface, so a token an agent passes to a job never comes back into
its own context via a status call.

## Pattern 1 — fire, then babysit cheaply

The agent's context is expensive; a training run is hours long. The pattern is
submit → detach → poll on a widening cadence:

1. `jobd_submit` with the resource truth: `{"cmd": ["python", "train.py"],
   "project": "myproj", "requires": {"gpu": true}, "vram_gb": 16,
   "max_wall_s": 14400, "idle_timeout_s": 900}`. The response carries the job
   id **and an ETA estimate** (p50/p90 from history) — use it to set the first
   check-in instead of guessing.
2. `jobd_status` at the ETA's p50, then p90, then on a backoff. The status
   includes `state`, `worker`, timing, and any `warning` the broker stamped
   (e.g. why it's still queued).
3. On `state: "failed"` — `jobd_logs` with a tail is usually enough for the
   agent to diagnose and resubmit with a fix. The watchdogs make failure
   modes legible: `termination_reason` distinguishes `wall_timeout`,
   `idle_timeout`, and `first_output_timeout` from an ordinary non-zero exit.

Set `idle_timeout_s` on anything an agent submits. An agent that silently
waits on a silently-hung job is two bugs stacked; the idle watchdog converts
the hang into a `failed` state the agent can actually react to.

## Pattern 2 — survive preemption instead of fearing it

A higher-priority job (yours, or another agent's) can preempt a running one.
The contract:

1. The worker sends `SIGTERM` and sets `JOBD_CHECKPOINT_GRACE_S` in the
   workload's environment (default 60s, cap 300s via `checkpoint_grace_s` at
   submit).
2. The workload checkpoints to `JOBD_CHECKPOINT_DIR` (a durable per-job
   directory the worker creates) and prints the `jobd-checkpoint-complete`
   sentinel. In Python this is three lines:

   ```python
   from jobd.client import install_preemption_handler
   install_preemption_handler(lambda remaining: save_checkpoint(ckpt_dir))
   ```

3. The job lands in a terminal `preempted` state — **not** silently re-run.

The agent's move on seeing `preempted`: resubmit the same command with a
`--resume`-style flag pointing at the checkpoint dir, at the same or bumped
priority. Because the state is terminal and the checkpoint is durable, this
is a plain conditional, not a distributed-systems puzzle.

## Pattern 3 — sweeps, then collect

`jobd_submit` accepts `count` (N members, `{i}` substituted) or `sweep` axes
(`[{"key": "lr", "values": ["0.1", "0.01"]}]`, cartesian product). Submit the
array, then `jobd_list` filtered to the array to watch members complete;
failed members carry their own logs. Dependencies (`depends_on`) let an agent
chain a collect/report job that only runs after the sweep finishes — with
`depends_on_any_exit` if the report should run even over partial failures.

## Pattern 4 — when a job won't schedule, ask why

A queued job that isn't starting has an answer, not a mystery:

- `jobd_status` surfaces broker warnings (`unmatcheable: ...`,
  `blocked: ...`) stamped by the sweep.
- `jobd_workers` shows live capacity — free VRAM/RAM/CPUs, tags, state — so
  the agent can see *which* requirement doesn't fit and either relax it or
  `jobd_preempt` a preemptible blocker.
- `jobd_events` is the flight recorder: `dispatch_skip` events carry the
  per-worker reason a job was passed over.

## Pattern 5 — post-mortem without re-running

`jobd_events` filtered by `job_id` reconstructs a job's whole life:
submitted → dispatched → started → (watchdog_fired?) → terminal, with
timestamps and reasons. For "what happened while I was away", filter by event
type instead: `job_orphaned`, `worker_offline`, `auto_preempt`,
`version_drift`.

## Ground rules for agent prompts

Worth encoding in your agent's instructions (CLAUDE.md or equivalent):

- Always set `project` (routing, priorities, and accounting hang off it) and
  prefer per-project defaults in `projects.yaml` over per-submit flags.
- Always set `idle_timeout_s` / `max_wall_s` for unattended work.
- Prefer `jobd_cancel` over abandoning a job — an abandoned job holds a GPU.
- Don't `jobd_worker_delete` on a whim: it drains a live host's registration,
  and it is the one destructive tool in the set.
- Multiple agents sharing one fleet don't need coordination — that is the
  broker's job. Give each agent its own `project` and let priorities arbitrate.
