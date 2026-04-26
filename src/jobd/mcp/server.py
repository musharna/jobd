"""stdio MCP server for jobd. Boilerplate; tools wired in Task 23."""

from __future__ import annotations

import asyncio
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server


def build_server() -> Server:
    """Construct the MCP Server. Tool registrations added in Task 23."""
    return Server("jobd")


async def _run() -> None:
    server = build_server()
    print(f"jobd-mcp ready (JOBD_URL={os.environ.get('JOBD_URL', 'unset')})", file=sys.stderr)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
