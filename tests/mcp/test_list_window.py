"""jobd_list's finished_at window (finished_after / finished_before).

The broker's GET /jobs has no time filter, so the MCP pages through the
state/project-filtered set and filters it here. The mock below honours the
real endpoint's contract (state_filter, project, limit, offset, newest first,
X-Total-Count over the filtered set), so a window that only looked at the
first page, or a count taken from the page instead of the window, fails.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from mcp.client import Client

from jobd.client import JobdClient
from jobd.mcp import tools
from jobd.mcp.server import build_server
from jobd.mcp.tools import ArgumentError, jobd_list, parse_when

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _row(i: int, state: str, finished: datetime | None, project: str = "p") -> dict:
    started = finished - timedelta(seconds=30 + i) if finished else None
    return {
        "id": i,
        "project": project,
        "state": state,
        "worker": "laptop" if finished else None,
        "exit_code": 0 if state == "completed" else 1,
        "submitted_at": (T0 - timedelta(days=30)).isoformat(),
        "started_at": started.isoformat() if started else None,
        "finished_at": finished.isoformat() if finished else None,
    }


def _mock(rows: list[dict], insert_after_first_page: dict | None = None):
    rows = sorted(rows, key=lambda r: r["id"], reverse=True)
    calls = {"n": 0}

    def responder(request):
        nonlocal rows
        p = request.url.params
        calls["n"] += 1
        if insert_after_first_page and calls["n"] == 2:
            rows = [insert_after_first_page, *rows]  # a submit lands mid-scan
        out = [
            r
            for r in rows
            if (not p.get("state_filter") or r["state"] == p["state_filter"])
            and (not p.get("project") or r["project"] == p["project"])
        ]
        total = len(out)
        off = int(p.get("offset", 0))
        out = out[off:]
        if p.get("limit"):
            out = out[: int(p["limit"])]
        return httpx.Response(200, json=out, headers={"X-Total-Count": str(total)})

    return respx.get("http://broker.test/jobs").mock(side_effect=responder)


def _fleet() -> list[dict]:
    """2,500 jobs, one finishing every minute from T0, cycling through states;
    plus queued jobs with no finished_at. Three pages at the broker's cap."""
    states = ["completed", "failed", "cancelled", "completed", "preempted"]
    rows = [_row(i, states[i % 5], T0 + timedelta(minutes=i)) for i in range(1, 2501)]
    rows += [_row(i, "queued", None) for i in range(2501, 2511)]
    return rows


def _expected(rows, after, before, states=None):
    return [
        r
        for r in rows
        if r["finished_at"]
        and (states is None or r["state"] in states)
        and after <= datetime.fromisoformat(r["finished_at"]) < before
    ]


@respx.mock
def test_window_counts_cover_every_page_and_the_bounds_are_half_open():
    rows = _fleet()
    route = _mock(rows)
    after, before = T0 + timedelta(minutes=100), T0 + timedelta(minutes=2400)
    client = JobdClient(base_url="http://broker.test")
    out = jobd_list(
        client,
        {"finished_after": after.isoformat(), "finished_before": before.isoformat(), "limit": 200},
    )
    want = _expected(rows, after, before)
    assert sum(out["counts"].values()) == len(want) == 2300
    # Per state, zeroes included (orphaned, scheduling_timeout never occur).
    for s in tools.TERMINAL_STATES:
        assert out["counts"][s] == sum(r["state"] == s for r in want), s
    assert out["counts"]["orphaned"] == 0
    # Minute 100 is in (inclusive start), minute 2400 is out (exclusive end).
    ids = {j["job_id"] for j in out["jobs"]}
    assert max(ids) == 2399 and 2400 not in ids
    assert out["truncated"] == 2300 - 200
    assert out["window"]["finished_after"] == after.isoformat()
    # The rows carry the fields a window question needs.
    j = out["jobs"][0]
    assert j["finished_at"] and j["duration_s"] == pytest.approx(30 + j["job_id"])
    # Every request was bounded at the broker's page cap and paged by offset.
    offsets = [int(c.request.url.params.get("offset", 0)) for c in route.calls]
    assert offsets == [0, 1000, 2000]
    assert all(c.request.url.params["limit"] == "1000" for c in route.calls)


@respx.mock
def test_a_job_on_the_last_page_is_found_and_the_first_page_alone_is_not_enough():
    """Positive control for paging: the only match is on page 3. With paging
    broken (one request), the count would be 0."""
    rows = _fleet()
    _mock(rows)
    after, before = T0 + timedelta(minutes=3), T0 + timedelta(minutes=4)  # job 3 only
    out = jobd_list(
        JobdClient(base_url="http://broker.test"),
        {"finished_after": after.isoformat(), "finished_before": before.isoformat()},
    )
    assert [j["job_id"] for j in out["jobs"]] == [3]
    assert out["counts"]["completed"] == 1 and sum(out["counts"].values()) == 1


@respx.mock
def test_one_state_is_filtered_by_the_broker_and_project_is_forwarded():
    rows = _fleet() + [_row(9000, "failed", T0 + timedelta(minutes=50), project="other")]
    route = _mock(rows)
    out = jobd_list(
        JobdClient(base_url="http://broker.test"),
        {"state": ["failed"], "project": "p", "finished_after": T0.isoformat()},
    )
    assert out["counts"] == {"failed": 500}
    assert 9000 not in {j["job_id"] for j in out["jobs"]}
    for c in route.calls:
        assert c.request.url.params["state_filter"] == "failed"
        assert c.request.url.params["project"] == "p"


@respx.mock
def test_a_row_repeated_by_a_mid_scan_submit_is_counted_once():
    rows = _fleet()
    # A new job at the top shifts every row down one place, so the last row of
    # page 1 comes back as the first row of page 2.
    _mock(rows, insert_after_first_page=_row(5000, "queued", None))
    out = jobd_list(JobdClient(base_url="http://broker.test"), {"finished_after": T0.isoformat()})
    assert sum(out["counts"].values()) == 2500


@respx.mock
def test_scan_past_the_cap_refuses_instead_of_undercounting(monkeypatch):
    _mock(_fleet())
    monkeypatch.setattr(tools, "_WINDOW_MAX_PAGES", 2)
    with pytest.raises(ArgumentError, match="window scan passed 2000 jobs"):
        jobd_list(JobdClient(base_url="http://broker.test"), {"finished_after": "2026-01-01"})


def test_parse_when_reads_relative_iso_and_naive_as_utc():
    now = datetime(2026, 10, 18, 12, tzinfo=UTC)
    assert parse_when("x", "3d", now) == now - timedelta(days=3)
    assert parse_when("x", "2h", now) == now - timedelta(hours=2)
    assert parse_when("x", "1w", now) == now - timedelta(weeks=1)
    assert parse_when("x", "2026-10-14T06:00:00Z") == datetime(2026, 10, 14, 6, tzinfo=UTC)
    assert parse_when("x", "2026-10-14T06:00:00") == datetime(2026, 10, 14, 6, tzinfo=UTC)
    assert parse_when("x", "2026-10-14T02:00:00-04:00") == datetime(2026, 10, 14, 6, tzinfo=UTC)
    for bad in ("yesterday", "0h", "-3d", ""):
        with pytest.raises(ArgumentError):
            parse_when("finished_after", bad, now)


def _call(server, name: str, args: dict):
    async def go():
        async with Client(server) as cl:
            return await cl.call_tool(name, args)

    r = asyncio.run(go())
    return r.is_error, json.loads(r.content[0].text)


@respx.mock
def test_bad_window_is_invalid_arguments_over_the_protocol_and_a_good_one_succeeds():
    _mock(_fleet())
    server = build_server(client=JobdClient(base_url="http://broker.test"))
    is_err, body = _call(server, "jobd_list", {"finished_after": "last tuesday"})
    assert is_err and body["error"]["kind"] == "invalid_arguments"
    assert "not a timestamp" in body["error"]["message"]
    is_err, body = _call(
        server,
        "jobd_list",
        {"finished_after": "2026-10-02T00:00:00Z", "finished_before": "2026-10-01T00:00:00Z"},
    )
    assert is_err and "empty window" in body["error"]["message"]
    # Positive control in the same harness: a valid window answers.
    is_err, body = _call(
        server, "jobd_list", {"finished_after": T0.isoformat(), "state": ["preempted"]}
    )
    assert not is_err and body["counts"] == {"preempted": 500}


@respx.mock
def test_no_window_keeps_the_bounded_active_set_path():
    """The window must not change the default call: one bounded request per
    active state, no offset paging."""
    route = _mock(_fleet())
    out = jobd_list(JobdClient(base_url="http://broker.test"), {})
    assert out["counts"] == {"queued": 10, "assigned": 0, "running": 0}
    assert "window" not in out
    assert all(c.request.url.params.get("state_filter") for c in route.calls)
    assert not any(c.request.url.params.get("offset") for c in route.calls)
