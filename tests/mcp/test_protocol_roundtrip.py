"""End-to-end MCP protocol round-trips against the real server object.

Every other test in this directory reaches past the protocol: they call
`jobd.mcp.tools` functions directly, or use the `server._jobd_dispatch` escape
hatch. That means the whole suite stayed green through the mcp 1.x -> 2.x
migration even though the migration rewrote how handlers are registered
(`@server.list_tools()` decorators -> `on_list_tools=` constructor arguments)
and what they must return (`ListToolsResult` / `CallToolResult` instead of bare
lists). A broken registration would not have failed a single existing test.

These drive `mcp.client.Client`, which accepts a `Server` directly and speaks
the real protocol in-process, so the handler signatures and result wrapping are
exercised rather than assumed.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import respx
from mcp.client import Client

from jobd.client import JobdClient
from jobd.mcp.server import _TOOLS, build_server


def _run(coro):
    return asyncio.run(coro)


@respx.mock
def test_list_tools_over_the_protocol_returns_every_registered_tool():
    server = build_server(client=JobdClient(base_url="http://broker.test"))

    async def go():
        async with Client(server) as client:
            return await client.list_tools()

    result = _run(go())

    names = {tool.name for tool in result.tools}
    assert names == {name for name, *_ in _TOOLS}, names

    # input_schema is the 2.x spelling of inputSchema. Constructing a Tool with
    # the wrong one raises, but only when a handler actually builds it - which
    # nothing did before this test existed.
    for tool in result.tools:
        assert tool.input_schema, f"{tool.name} lost its input schema"
        assert tool.description


@respx.mock
def test_call_tool_over_the_protocol_returns_the_broker_payload():
    respx.get("http://broker.test/jobs/7").mock(
        return_value=httpx.Response(200, json={"job_id": 7, "state": "running"})
    )
    server = build_server(client=JobdClient(base_url="http://broker.test"))

    async def go():
        async with Client(server) as client:
            return await client.call_tool("jobd_status", {"job_id": 7})

    result = _run(go())

    assert not result.is_error
    payload = json.loads(result.content[0].text)
    assert payload["job_id"] == 7
    assert payload["state"] == "running"


@respx.mock
def test_transport_failure_surfaces_as_is_error_not_a_crash():
    """Broker down -> is_error=True, per tests/mcp/walkthrough.md step 11.

    mcp 1.x turned an exception raised inside the handler into isError=true.
    2.x removed that conversion, so the server has to build the error result
    itself; if it ever goes back to raising, this fails instead of silently
    changing the client-visible contract.
    """
    respx.get("http://broker.test/jobs/7").mock(
        return_value=httpx.Response(200, json={"job_id": 7, "state": "running"})
    )
    respx.get("http://broker.test/workers").mock(side_effect=httpx.ConnectError("refused"))
    server = build_server(client=JobdClient(base_url="http://broker.test"))

    async def go():
        async with Client(server) as client:
            bad = await client.call_tool("jobd_workers", {})
            # Positive control in the SAME test: a broken harness that failed
            # every call would otherwise read as "the error path works".
            good = await client.call_tool("jobd_status", {"job_id": 7})
            return bad, good

    bad, good = _run(go())

    assert bad.is_error, "broker outage must surface as is_error, not an exception"
    assert "transport" in bad.content[0].text.lower() or "jobd" in bad.content[0].text.lower()
    assert not good.is_error, "positive control failed - the harness itself is broken"
    assert json.loads(good.content[0].text)["job_id"] == 7


@respx.mock
def test_unknown_tool_is_an_error_result_not_an_exception():
    server = build_server(client=JobdClient(base_url="http://broker.test"))

    async def go():
        async with Client(server) as client:
            return await client.call_tool("jobd_does_not_exist", {})

    result = _run(go())

    assert result.is_error
    assert "unknown tool" in result.content[0].text.lower()
