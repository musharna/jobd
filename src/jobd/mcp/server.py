"""stdio MCP server for jobd."""

from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from jobd.client import BrokerRefusal, BrokerServerError, BrokerUnreachable, JobdClient
from jobd.mcp import tools as t
from jobd.mcp.errors import map_broker_refusal
from jobd.mcp.schemas import (
    CANCEL_INPUT,
    JOB_ID_ONLY,
    LIST_INPUT,
    LOGS_INPUT,
    STATUS_INPUT,
    SUBMIT_INPUT,
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
        "Get JobInfo for a job_id. Pass wait=true to block until terminal or wait_timeout_s.",
        STATUS_INPUT,
        t.jobd_status,
    ),
    (
        "jobd_logs",
        "Tail the captured stdout/stderr of a job (broker streams logs to disk).",
        LOGS_INPUT,
        t.jobd_logs,
    ),
    (
        "jobd_cancel",
        "Cancel a job (queued → cancelled; running → SIGTERM via worker signal poll, ~2s).",
        CANCEL_INPUT,
        t.jobd_cancel,
    ),
    ("jobd_list", "List queue + recent jobs with counts.", LIST_INPUT, t.jobd_list),
    (
        "jobd_workers",
        "Fleet snapshot with health rollup (healthy|degraded|empty).",
        WORKERS_INPUT,
        t.jobd_workers,
    ),
    (
        "jobd_job_get",
        "Full JobInfo for a single job_id (deps, signals, profile, mount_roots, fast_path).",
        JOB_ID_ONLY,
        t.jobd_job_get,
    ),
]


def build_server(client: JobdClient | None = None) -> Server:
    server = Server("jobd")
    client = client or JobdClient()

    def _dispatch(name: str, arguments: dict) -> dict:
        """Synchronous tool dispatch. Maps BrokerRefusal → structured error.

        Transport errors (BrokerUnreachable / BrokerServerError) propagate so the
        MCP layer can surface them as isError=true.
        """
        for tname, _, _, fn in _TOOLS:
            if tname == name:
                try:
                    return fn(client, arguments)
                except BrokerRefusal as e:
                    return {"error": map_broker_refusal(e)}
        raise ValueError(f"unknown tool: {name}")

    server._jobd_dispatch = _dispatch  # type: ignore[attr-defined]

    @server.list_tools()
    async def _list() -> list[types.Tool]:
        return [
            types.Tool(name=name, description=desc, inputSchema=schema)
            for name, desc, schema, _ in _TOOLS
        ]

    @server.call_tool()
    async def _call(name: str, arguments: dict) -> list[types.TextContent]:
        try:
            payload = _dispatch(name, arguments)
        except (BrokerUnreachable, BrokerServerError) as e:
            raise RuntimeError(f"jobd transport error: {e}") from e
        return [types.TextContent(type="text", text=json.dumps(payload))]

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
