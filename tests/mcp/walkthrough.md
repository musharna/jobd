# jobd MCP — live-broker walkthrough recipe

Run this against the live server broker before tagging a release and after any broker schema change.
Reason: synthetic-fixture unit tests cannot see schema drift, transport edge cases, or live behaviors
(`feedback_real_execution_testing.md` doctrine).

## Pre-flight

```bash
JOBD_URL=http://127.0.0.1:8765 jobd-mcp &
JOBD_MCP_PID=$!
sleep 1
```

Confirm `jobd-mcp ready (JOBD_URL=...)` on stderr.

## Smoke through 7 tools (from a Claude session)

1. **`jobd_workers`** → expect ≥1 worker, `fleet_health: "healthy"`.
2. **`jobd_submit`** async, `cpu-quick` profile (via `extra.profile`), `command: "echo hello && sleep 5"` → returns `job_id`, `state ∈ {queued, assigned}`.
3. **`jobd_status(job_id)`** repeated → converges to `state: completed`, `exit_code: 0`.
4. **`jobd_logs(job_id)`** → `tail` includes `hello`.
5. **`jobd_submit`** sync (`wait=true`, `command: "sleep 3"`) → returns terminal in one call with `log_tail`.
6. **`jobd_submit`** sync timeout (`wait=true wait_timeout_s=10 command: "sleep 30"`) → `state: running`, `timed_out: true`.
   - Then **`jobd_cancel(job_id)`** → `prior_state: running`, signal sent.
   - Then **`jobd_status(job_id)`** within ~2-3s → `state: cancelled`.
   - **Important:** the cancel target must still be running when the cancel call lands. A trivial command (`true`, `echo hi`) finishes in tens of milliseconds — by the time `jobd_cancel` arrives, prior_state is already `completed` and `signal_sent: null`. That's correct behavior (cancel-of-terminal is a no-op contract), but it doesn't exercise the cancel-while-running path. Use `sleep 30` or longer for this step.
7. **`jobd_list`** → previous jobs visible.
8. **`jobd_submit`** with `extra.depends_on=[<recent_completed_id>]` → child queued; **`jobd_status(child_id)`** shows the dep.

## Refusal cases

9. `jobd_submit cwd=/mnt/c/Users/x command=true project=p` (no `host` set, no laptop worker) →
   tool result `{error: {kind: "cwd_outside_mount_roots" or "no_eligible_worker", hint, message}}`.
10. `jobd_status(job_id=99999999)` → `{error: {kind: "not_found", ...}}`.

## Transport failure

11. Stop broker (or block Tailscale) → `jobd_workers` returns MCP `isError=true`, hint mentions `JOBD_URL`.
    Restart broker, retry, confirm recovery.

## Cleanup

```bash
kill $JOBD_MCP_PID
```

## Sign-off

Capture outputs into a session note (paste into the conversation that runs the walkthrough).
Then tag and update memory (Task 29).
