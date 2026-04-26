# jobd MCP

Stdio MCP server exposing the jobd broker to Claude Code sessions.

## Install (laptop)

```bash
git clone gt76:~/jobd ~/jobd # or use existing local checkout
cd ~/jobd
pip install -e '.[mcp]'
```

## Register

In `~/.claude.json` `mcpServers`:

```json
"jobd": {
  "command": "jobd-mcp",
  "env": { "JOBD_URL": "http://100.113.204.41:8765" }
}
```

## Tools (7)

| Tool           | Purpose                                                                               |
| -------------- | ------------------------------------------------------------------------------------- |
| `jobd_submit`  | Submit a job; async by default, `wait=true` for short-block (server clamps to 270 s). |
| `jobd_status`  | Full JobInfo; `wait=true` blocks to terminal or timeout.                              |
| `jobd_logs`    | Tail of captured output (default 8 KiB; max 1 MiB).                                   |
| `jobd_cancel`  | Queued → cancelled; running → SIGTERM via worker signal poll (~2 s).                  |
| `jobd_list`    | Queue + recent jobs with counts.                                                      |
| `jobd_workers` | Fleet snapshot + health rollup.                                                       |
| `jobd_job_get` | Full JobInfo for one job (deps, profile, fast_path).                                  |

See `docs/superpowers/specs/2026-04-26-jobd-mcp-design.md` for full contract.

## Errors

- Transport failures (broker unreachable, 5xx) → MCP `isError=true` with hint mentioning `JOBD_URL`.
- Broker 4xx refusals → tool result `{error: {kind, message, hint}}`. Kinds: `invalid_submit`, `unknown_parent`, `cwd_outside_mount_roots`, `no_eligible_worker`, `not_found`, `conflict`, `unknown`.

## Tests

- Unit: `pytest tests/mcp/`
- Live walkthrough: see `tests/walkthrough.md`. Run before tagging a release.

## Observability

Each tool call appended to `~/.claude/state/jobd-mcp/calls.jsonl` (override path with `JOBD_MCP_LOG_DIR`).
