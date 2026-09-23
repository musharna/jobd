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

## Tools (8)

| Tool                 | Purpose                                                                               |
| -------------------- | ------------------------------------------------------------------------------------- |
| `jobd_submit`        | Submit a job; async by default, `wait=true` for short-block (server clamps to 270 s). |
| `jobd_status`        | Full JobInfo; `wait=true` blocks to terminal or timeout.                              |
| `jobd_logs`          | Tail of captured output (default 8 KiB; max 1 MiB).                                   |
| `jobd_cancel`        | Queued → cancelled; running → SIGTERM via worker signal poll (~2 s).                  |
| `jobd_preempt`       | Preempt a running/assigned job: SIGTERM with checkpoint grace, terminal `preempted`.  |
| `jobd_list`          | Queue + recent jobs with counts.                                                      |
| `jobd_workers`       | Fleet snapshot + health rollup (incl. each worker's jobd `version`).                  |
| `jobd_worker_delete` | Purge a stale worker registration by host.                                            |

## Errors

- Arguments are validated against each tool's `inputSchema` before the tool runs; every schema is closed (`additionalProperties: false`). A missing, mistyped, unknown or misplaced key → `isError=true` with `{error: {kind: "invalid_arguments", message, hint}}` — never a silent drop.
- Transport failures (any httpx transport error: connect, timeouts incl. write/pool, protocol) → `isError=true`, text naming the failure kind plus a hint.
- Any other failure inside the server (or an unparseable broker reply) → `isError=true` with kind `internal_error`; an unregistered tool name → `unknown_tool`. All are recorded in the call log's `error_kind`.
- Broker 4xx refusals → tool result `{error: {kind, message, hint}}`. Kinds: `invalid_submit`, `unknown_parent`, `parent_failed`, `cwd_outside_mount_roots`, `bad_request`, `not_found`, `conflict`, `auth_failed`, `forbidden`, `invalid_arguments`, `unknown`. 404/409 hints name the tool's resource (job vs worker).

`gpu` on `jobd_submit` is a pin flag, like the CLI's `--gpu`: `true` requires a GPU worker; `false` or omitted means no preference. Forbidding GPU workers (CLI `--no-gpu`) is not exposed over MCP.

## Tests

- Unit: `pytest tests/mcp/`
- Live walkthrough: see `tests/mcp/walkthrough.md`. Run before tagging a release.

## Observability

Each tool call appended to `~/.claude/state/jobd-mcp/calls.jsonl` (override path with `JOBD_MCP_LOG_DIR`).
