# jobd MCP

Stdio MCP server exposing the jobd broker to Claude Code sessions.

## Install

```bash
pip install 'jobd[mcp]'   # or, from a checkout: pip install -e '.[mcp]'
```

## Register

In `~/.claude.json` `mcpServers`:

```json
"jobd": {
  "command": "jobd-mcp",
  "env": { "JOBD_URL": "http://127.0.0.1:8765" }
}
```

## Tools (9)

| Tool                 | Purpose                                                                               |
| -------------------- | ------------------------------------------------------------------------------------- |
| `jobd_submit`        | Submit a job; async by default, `wait=true` for short-block (server clamps to 270 s). |
| `jobd_status`        | Full JobInfo; `wait=true` blocks to terminal or timeout.                              |
| `jobd_logs`          | Tail of captured output (default 8 KiB; max 1 MiB).                                   |
| `jobd_cancel`        | Queued → cancelled; running → SIGTERM via worker signal poll (~2 s).                  |
| `jobd_preempt`       | Preempt a running/assigned job: SIGTERM with checkpoint grace, terminal `preempted`.  |
| `jobd_list`          | Queue + recent jobs with counts.                                                      |
| `jobd_workers`       | Fleet snapshot + health rollup.                                                       |
| `jobd_job_get`       | Full JobInfo for one job (deps, profile, fast_path).                                  |
| `jobd_worker_delete` | Purge a stale worker registration by host.                                            |

## Errors

- Transport failures (broker unreachable, 5xx) → MCP `isError=true` with hint mentioning `JOBD_URL`.
- Broker 4xx refusals → tool result `{error: {kind, message, hint}}`. Kinds: `invalid_submit`, `unknown_parent`, `cwd_outside_mount_roots`, `no_eligible_worker`, `not_found`, `conflict`, `unknown`.

## Tests

- Unit: `pytest tests/mcp/`
- Live walkthrough: see `tests/walkthrough.md`. Run before tagging a release.

## Observability

Each tool call appended to `~/.claude/state/jobd-mcp/calls.jsonl` (override path with `JOBD_MCP_LOG_DIR`).
