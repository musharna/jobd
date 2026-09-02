"""stdio MCP server for jobd."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server

from jobd.client import BrokerRefusal, BrokerServerError, BrokerUnreachable, JobdClient
from jobd.mcp import tools as t
from jobd.mcp.errors import map_broker_refusal
from jobd.mcp.schemas import (
    CANCEL_INPUT,
    EVENTS_INPUT,
    LIST_INPUT,
    LOGS_INPUT,
    PREEMPT_INPUT,
    STATUS_INPUT,
    SUBMIT_INPUT,
    WORKER_DELETE_INPUT,
    WORKERS_INPUT,
)

_TOOLS = [
    (
        "jobd_submit",
        "Submit a job to the jobd broker. Default async; pass wait=true to block up to wait_timeout_s (server clamps to 270).",
        SUBMIT_INPUT,
        t.jobd_submit,
    ),
    (
        "jobd_status",
        "The full JobInfo record for one job_id: state, exit_code, timings, host, and the scheduling internals — depends_on + cascade policy (depends_on_any_exit), pending cancel/preempt signal, resolved profile, requires (gpu/tags/idempotent), host pin, fast_path, timeouts, termination_reason. Use for a quick state check AND for debugging why a job routed/failed/stalled. Pass wait=true to block until terminal or wait_timeout_s.",
        STATUS_INPUT,
        t.jobd_status,
    ),
    (
        "jobd_logs",
        "Tail the captured stdout/stderr of a job (workers stream output to the broker's per-job log as it runs). Returns log_tail (last tail_bytes, default 8 KiB, max 1 MiB) plus size_bytes/returned_bytes/truncated — works for running AND finished jobs. Use to check progress mid-run, diagnose a failure's traceback, or grab a job's final output.",
        LOGS_INPUT,
        t.jobd_logs,
    ),
    (
        "jobd_cancel",
        "Cancel a job (queued → cancelled; running → SIGTERM via worker signal poll, ~2s).",
        CANCEL_INPUT,
        t.jobd_cancel,
    ),
    (
        "jobd_preempt",
        "Preempt a running/assigned preemptible job (worker SIGTERMs with grace; final state 'preempted'). Refused if not preemptible or not running.",
        PREEMPT_INPUT,
        t.jobd_preempt,
    ),
    (
        "jobd_list",
        "List jobs on the broker with per-state counts. Defaults to the active set (queued/assigned/running); filter by state (e.g. ['failed']) or project to find past runs. Each row is a compact summary: job_id, project, state, host, exit_code, queued_at, started_at — call jobd_status for a job's full record. Use to answer 'what is running / queued right now?' or to locate a job id you've lost.",
        LIST_INPUT,
        t.jobd_list,
    ),
    (
        "jobd_events",
        "The broker's event stream — the surface that explains WHY, not just what. /jobs says a job is queued; only this says it has been skipped 400 times because no worker advertises cuda-32gb, or that its dependency was cancelled, or that a watchdog killed it. Filter by since (2h/3d/1w), event type, job_id, project, or source (broker|worker). Use when a job is not doing what you expect and jobd_status alone does not explain it.",
        EVENTS_INPUT,
        t.jobd_events,
    ),
    (
        "jobd_workers",
        "Fleet snapshot: every registered worker with state (online/stale/offline), live capacity ad (free_vram_gb, unregistered_vram_gb, free_ram_gb, idle_cpus), capability tags (cuda tiers, arch/os), slot usage (running/max_concurrent), and last_heartbeat — plus an overall health rollup (healthy|degraded|empty). Use before submitting GPU work to see what's free, or to diagnose why a job isn't being dispatched.",
        WORKERS_INPUT,
        t.jobd_workers,
    ),
    # NOTE: there is deliberately no `jobd_job_get`. It existed until 2026-07-12
    # and was a byte-identical duplicate of jobd_status — both called
    # GET /jobs/{id} and returned the same translated dict — while its
    # description told the model it returned "everything jobd_status returns
    # PLUS scheduling internals". That was false: jobd_status already returns
    # the full JobInfo. Two names for one operation, one of them lying about the
    # difference, cost a ninth of the tool budget and actively misdirected tool
    # selection. jobd_status's description now carries the accurate half.
    (
        "jobd_worker_delete",
        "Remove a worker from the broker registry. The broker refuses (409) if the worker is still online — caller stops the worker process or waits for the heartbeat sweeper first.",
        WORKER_DELETE_INPUT,
        t.jobd_worker_delete,
    ),
]


def _log_call(name: str, arguments: dict | None, error_kind: str | None, ms: float) -> None:
    # Telemetry must never outrank the payload (audit 2026-07-15 Q-2): this is
    # invoked from a `finally` on the tool-call hot path, so an unguarded
    # OSError here (read-only HOME, full disk, sandboxed MCP process) would
    # replace every ALREADY-COMPUTED successful broker result with a logging
    # failure. Swallow filesystem errors; the call log is best-effort.
    try:
        log_dir = Path(
            os.environ.get("JOBD_MCP_LOG_DIR") or os.path.expanduser("~/.claude/state/jobd-mcp")
        )
        log_dir.mkdir(parents=True, exist_ok=True)
        entry: dict[str, Any] = {
            "ts": time.time(),
            "tool": name,
            "job_id": (arguments or {}).get("job_id"),
            "ms": round(ms, 1),
        }
        if error_kind:
            entry["error_kind"] = error_kind
        with (log_dir / "calls.jsonl").open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


_INSTRUCTIONS = """\
Cross-session job queue.

WHEN TO USE THIS SERVER:

Heavy / long-running compute (use when user wants to launch work that should outlive the current session):
- "run this overnight", "kick off training", "submit this job"
- Any command expected to run >5 minutes or use significant CPU/GPU/memory
- Anything the user wants babysat across sessions

GPU work (use when the work needs a specific GPU or arch):
- Pass --gpu and --needs cuda, --needs cuda-8gb, --needs cuda-24gb
- Routing matches against worker tags

Status / monitoring (use when user asks about prior jobs):
- "is job N done?", "what jobs are running", "show me the logs from job M"
- Tools: jobd_status, jobd_logs, jobd_list, jobd_workers

Job control (use when user wants to cancel, preempt, or inspect a specific job):
- "cancel job N", "preempt job M", "show full info for job N"
- Tools: jobd_cancel, jobd_preempt, jobd_status

NOT FOR:
- Quick (<30s) commands that block the session naturally — just use Bash
- Interactive workflows that need stdin (jobd is batch-only)
- Cron-like scheduling (use the schedule skill instead)

Use submitted_via="mcp" (auto-set) so observability can distinguish MCP-driven submissions from CLI-driven.
"""


def build_server(client: JobdClient | None = None) -> Server:
    client = client or JobdClient()

    def _dispatch(name: str, arguments: dict) -> dict:
        """Synchronous tool dispatch. Maps BrokerRefusal → structured error.

        Transport errors (BrokerUnreachable / BrokerServerError) propagate so the
        caller can surface them as is_error=True.
        """
        for tname, _, _, fn in _TOOLS:
            if tname == name:
                try:
                    return fn(client, arguments)
                except BrokerRefusal as e:
                    return {"error": map_broker_refusal(e)}
        raise ValueError(f"unknown tool: {name}")

    # mcp 2.x removed the @server.list_tools() / @server.call_tool() decorators.
    # Handlers are constructor arguments now, take (ctx, params), and must return
    # a Result object — a bare list is rejected.
    async def _list(
        ctx: ServerRequestContext,
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(name=name, description=desc, input_schema=schema)
                for name, desc, schema, _ in _TOOLS
            ]
        )

    async def _call(
        ctx: ServerRequestContext,
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        name = params.name
        arguments = params.arguments or {}
        t0 = time.monotonic()
        error_kind: str | None = None
        try:
            # Off the event loop: `_dispatch` drives a BLOCKING httpx client,
            # and a `jobd_submit wait=true` can hold it for up to 270 s. Run
            # inline, that stalled every other request on the stdio session
            # (audit 2026-09-02). Unknown-tool ValueError is raised inside the
            # thread too and propagates through `to_thread` unchanged.
            payload = await asyncio.to_thread(_dispatch, name, arguments)
            if (
                isinstance(payload, dict)
                and "error" in payload
                and isinstance(payload["error"], dict)
            ):
                error_kind = payload["error"].get("kind")
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(payload))]
            )
        except (BrokerUnreachable, BrokerServerError) as e:
            # Under 1.x an exception raised here was converted by the MCP layer
            # into isError=true. 2.x removed that conversion, so the result has
            # to be built explicitly — otherwise a broker outage propagates as a
            # protocol-level crash instead of the documented is_error response
            # (tests/mcp/walkthrough.md step 11).
            # Name WHICH transport failure, and pass the client's hint through.
            # An agent cannot ssh to the host to check whether the broker is
            # actually down, so it depends on this steer more than a human
            # operator does: from a bare "timed out" the obvious inference is
            # "the broker is down", which is precisely wrong for a dropped
            # packet and can lead an agent to restart a service that is fine.
            kind = getattr(e, "kind", None)
            hint = getattr(e, "hint", "")
            error_kind = f"transport_{kind}" if kind else "transport"
            text = f"jobd transport error ({kind}): {e}" if kind else f"jobd transport error: {e}"
            if hint:
                text = f"{text}\nhint: {hint}"
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=text)],
                is_error=True,
            )
        except ValueError as e:
            # Unknown tool name — same story, 1.x turned this into isError=true.
            error_kind = "unknown_tool"
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(e))],
                is_error=True,
            )
        except (KeyError, TypeError) as e:
            # A missing or mis-typed argument (`jobd_status {}`) raises inside
            # the tool. Uncaught, 2.x reports it as a protocol-level "Internal
            # server error" with a traceback on stderr, and the is_error + hint
            # contract silently stops applying to the most common agent
            # mistake (audit 2026-09-02). Same shape as a broker 422.
            error_kind = "invalid_arguments"
            payload = {
                "error": {
                    "kind": error_kind,
                    "message": f"missing or malformed argument: {e}",
                    "hint": f"check the inputSchema for {name}: required fields and types",
                }
            }
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(payload))],
                is_error=True,
            )
        finally:
            _log_call(name, arguments, error_kind, (time.monotonic() - t0) * 1000)

    server = Server(
        "jobd",
        instructions=_INSTRUCTIONS,
        on_list_tools=_list,
        on_call_tool=_call,
    )
    server._jobd_dispatch = _dispatch  # type: ignore[attr-defined]

    return server


async def _run() -> None:
    server = build_server()
    print(f"jobd-mcp ready (JOBD_URL={os.environ.get('JOBD_URL', 'unset')})", file=sys.stderr)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
