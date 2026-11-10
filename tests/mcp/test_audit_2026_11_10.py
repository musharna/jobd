"""Regression tests for the 2026-11-10 jobd-mcp audit (J-H1, J-L, B4).

Same rules as test_audit_2026_09_22: every refusal is paired with a positive
control in the same test; agent-visible claims go over the real MCP protocol;
broker-side claims use the real FastAPI app behind TestClient. The B4 check
starts the real `jobd-mcp` process and speaks MCP to it over stdio.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter

import httpx
import respx

from jobd import __version__
from jobd.client import JobdClient
from jobd.mcp.server import build_server
from jobd.models import JobState
from tests.mcp.test_audit_2026_09_22 import BROKER, _call, _hb, _real_broker_client

_SUBMIT = {"project": "p", "cwd": "/tmp"}


def _rig(app):
    c = _real_broker_client(app)
    _hb(c._client, "box")
    return c, c._client, build_server(client=c)


# --- J-H1: state=[] counts must cover every job, not the returned page -------


def test_jh1_all_states_counts_cover_every_job_not_just_the_page(app):
    c, tc, server = _rig(app)
    ids = []
    for i in range(12):
        e, out = _call(server, "jobd_submit", {"command": f"echo {i}", **_SUBMIT})
        assert not e, out
        ids.append(out["job_id"])
    # Cancel the 8 OLDEST, so the newest page of 3 is all `queued` and a
    # page-only count would miss `cancelled` entirely.
    for j in ids[:8]:
        e, out = _call(server, "jobd_cancel", {"job_id": j})
        assert not e, out
    truth = Counter(r["state"] for r in tc.get("/jobs").json())
    assert truth == {"queued": 4, "cancelled": 8}, truth  # the rig did what we think

    limits: list[int | None] = []
    real = c.list_jobs_with_total

    def spy(**kw):
        limits.append(kw.get("limit"))
        return real(**kw)

    c.list_jobs_with_total = spy  # type: ignore[method-assign]

    e, out = _call(server, "jobd_list", {"state": [], "limit": 3})
    assert not e, out
    assert {k: v for k, v in out["counts"].items() if v} == dict(truth), out["counts"]
    # Every state is reported, zeroes included, as for a named-state call.
    assert set(out["counts"]) == {s.value for s in JobState}, out["counts"]
    assert [j["job_id"] for j in out["jobs"]] == sorted(ids, reverse=True)[:3]
    assert out["truncated"] == 9, out
    # Exact counts without pulling the history: every broker request is bounded.
    assert limits and all(lim is not None and lim <= 3 for lim in limits), limits

    # Positive control: naming the states gave the right counts before the fix.
    e, out = _call(server, "jobd_list", {"state": ["queued", "cancelled"], "limit": 3})
    assert not e and out["counts"] == dict(truth), out


# --- J-L: broker refusals are tool errors (isError=true) -----------------------


def test_jl_broker_refusals_set_is_error(app):
    c, tc, server = _rig(app)
    for name, args, kind in [
        ("jobd_status", {"job_id": 999999}, "not_found"),
        ("jobd_cancel", {"job_id": 999999}, "not_found"),
        ("jobd_worker_delete", {"host": "box"}, "conflict"),  # still online
        ("jobd_worker_delete", {"host": "nope"}, "not_found"),
        ("jobd_events", {"since": "banana"}, "invalid_submit"),
    ]:
        e, out = _call(server, name, args)
        assert e, f"{name} {args}: a broker refusal came back as a success: {out}"
        assert out["error"]["kind"] == kind, out
        assert out["error"]["hint"], out
    # Positive control: the same tools on things that exist are not errors.
    e, out = _call(server, "jobd_submit", {"command": "true", **_SUBMIT})
    assert not e, out
    e, out = _call(server, "jobd_status", {"job_id": out["job_id"]})
    assert not e and out["state"] == "queued", out
    e, out = _call(server, "jobd_events", {"since": "1h"})
    assert not e and out["events"], out


# --- J-L: jobd_submit refuses an empty command and a negative wait ------------


def test_jl_submit_refuses_empty_command_and_negative_wait(app):
    c, tc, server = _rig(app)
    for args in [
        {"command": "", **_SUBMIT},
        {"command": "   \n", **_SUBMIT},
        {"command": "true", **_SUBMIT, "wait": True, "wait_timeout_s": -3},
    ]:
        e, out = _call(server, "jobd_submit", args)
        assert e and out["error"]["kind"] == "invalid_arguments", (args, out)
    e, out = _call(server, "jobd_status", {"job_id": 1, "wait": True, "wait_timeout_s": -1})
    assert e and out["error"]["kind"] == "invalid_arguments", out
    assert tc.get("/jobs").json() == [], "a refused submit still queued a job"
    # Positive control: a real command with a zero wait is accepted and queued.
    e, out = _call(server, "jobd_submit", {"command": "true", **_SUBMIT})
    assert not e and out["state"] == "queued", out
    e, out = _call(
        server, "jobd_status", {"job_id": out["job_id"], "wait": True, "wait_timeout_s": 0}
    )
    assert not e and out["timed_out"] is True, out


# --- J-L: jobd_events is bounded like jobd_list --------------------------------


def test_jl_events_limit_is_bounded(app):
    from jobd.mcp.tools import _EVENTS_LIMIT_MAX

    c, tc, server = _rig(app)
    for _ in range(30):
        assert tc.post("/submit", json={"cmd": ["true"], **_SUBMIT}).status_code == 200
    n_all = len(tc.get("/events", params={"limit": 10000}).json())
    assert n_all > 30, n_all  # enough rows that a cap is observable
    for bad in (-1, 0, _EVENTS_LIMIT_MAX + 1, 1_000_000):
        e, out = _call(server, "jobd_events", {"limit": bad})
        assert e and out["error"]["kind"] == "invalid_arguments", (bad, out)
    # Positive controls: an in-range limit returns exactly that many rows, and
    # the max is accepted.
    e, out = _call(server, "jobd_events", {"limit": 5})
    assert not e and len(out["events"]) == 5, out
    e, out = _call(server, "jobd_events", {"limit": _EVENTS_LIMIT_MAX})
    assert not e and len(out["events"]) == min(n_all, _EVENTS_LIMIT_MAX), len(out["events"])


@respx.mock
def test_jl_events_tool_clamps_even_without_the_schema():
    """The schema refuses an out-of-range limit before the tool runs; the tool
    clamps too, so a direct call can never ask the broker for its 10000 rows."""
    from jobd.mcp.tools import _EVENTS_LIMIT_MAX, jobd_events

    # The cap itself has an upper edge: at ~250 B per event, 1000 rows is
    # already ~250 KB of context. Raising it is a decision, not a drift.
    assert 1 < _EVENTS_LIMIT_MAX <= 1000, _EVENTS_LIMIT_MAX
    route = respx.get(f"{BROKER}/events").mock(return_value=httpx.Response(200, json=[]))
    client = JobdClient(base_url=BROKER)
    jobd_events(client, {"limit": 1_000_000})
    jobd_events(client, {"limit": -1})
    jobd_events(client, {"limit": 7})
    sent = [int(call.request.url.params["limit"]) for call in route.calls]
    assert sent == [_EVENTS_LIMIT_MAX, 1, 7], sent


# --- B4: real version and tool annotations, over real stdio --------------------

_READ_ONLY = {"jobd_status", "jobd_logs", "jobd_list", "jobd_events", "jobd_workers"}
_DESTRUCTIVE = {"jobd_cancel", "jobd_preempt", "jobd_worker_delete"}


def test_b4_initialize_reports_version_and_tools_carry_annotations(tmp_path):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    # A broker that cannot be reached: initialize and tools/list never call it.
    # The env is built from scratch so no JOBD_URL/JOBD_API_TOKEN leaks in.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "JOBD_URL": "http://127.0.0.1:9",
        "JOBD_MCP_LOG_DIR": str(tmp_path / "mcp-log"),
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "jobd.mcp.server"], env=env)

    async def go():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await asyncio.wait_for(session.initialize(), 30)
                tools = await asyncio.wait_for(session.list_tools(), 30)
                return init, tools

    init, tools = asyncio.run(go())
    assert init.server_info.name == "jobd"
    assert init.server_info.version == __version__ and __version__, init.server_info
    by_name = {tl.name: tl for tl in tools.tools}
    assert len(by_name) == 9, sorted(by_name)
    for name, tool in by_name.items():
        ann = tool.annotations
        assert ann is not None, f"{name} has no annotations"
        if name in _READ_ONLY:
            assert ann.read_only_hint is True, (name, ann)
        else:
            assert ann.read_only_hint is False, (name, ann)
        assert ann.destructive_hint is (name in _DESTRUCTIVE), (name, ann)
    # Positive control on the split itself: it names every tool exactly once.
    assert _READ_ONLY | _DESTRUCTIVE | {"jobd_submit"} == set(by_name)
