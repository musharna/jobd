"""Tests for two hand mutants that survived the suite in the 2026-11-10 audit.

- cancel-signal-ignores-orphan: `jobd_cancel` must report signal_sent="cancel"
  when the broker arms a cancel on a worker-gone orphan (v0.5.53). Driven
  through the real broker: claim, kill the worker, sweep, then cancel over MCP.
- list-limit-max-off: `jobd_list` clamps `limit` to _LIST_LIMIT_MAX itself, not
  only through the schema, so a direct call cannot ask the broker for more.
"""

from __future__ import annotations

import httpx
import respx

from jobd.client import JobdClient
from jobd.mcp.server import build_server
from jobd.mcp.tools import jobd_list
from tests.mcp.test_audit_2026_09_22 import _call
from tests.unit.test_heartbeat_resurrect import _claim, _dead, _row


def test_cancel_on_a_worker_gone_orphan_reports_the_armed_signal(client):
    jc = JobdClient(base_url="http://testserver")
    jc._client = client
    server = build_server(client=jc)

    orphan = _claim(client, "laptop")
    _dead(client, "laptop")
    client.app.state.sweep_once()
    assert _row(client, orphan).termination_reason == "worker_died"  # rig sanity

    e, out = _call(server, "jobd_cancel", {"job_id": orphan})
    assert not e, out
    assert out["new_state"] == "orphaned", out
    assert _row(client, orphan).signal == "cancel"  # the broker did arm it
    assert out["signal_sent"] == "cancel", (
        "the broker armed a cancel on a resurrectable orphan but the tool said "
        f"no signal was sent: {out}"
    )

    # Control: a job cancelled while queued gets no signal, and says so.
    queued = client.post("/submit", json={"cmd": ["true"], "project": "p", "cwd": "/tmp"}).json()[
        "id"
    ]
    e, out = _call(server, "jobd_cancel", {"job_id": queued})
    assert not e and out["new_state"] == "cancelled", out
    assert out["signal_sent"] is None, out


def _mock_jobs(rows: list[dict]):
    def responder(request):
        params = request.url.params
        out = [
            r
            for r in rows
            if not params.get("state_filter") or r["state"] == params["state_filter"]
        ]
        total = len(out)
        if params.get("limit"):
            out = out[: int(params["limit"])]
        return httpx.Response(200, json=out, headers={"X-Total-Count": str(total)})

    return respx.get("http://broker.test/jobs").mock(side_effect=responder)


@respx.mock
def test_list_limit_is_clamped_by_the_tool_not_only_the_schema():
    rows = [{"id": i, "project": "p", "state": "queued"} for i in range(300, 0, -1)]
    route = _mock_jobs(rows)
    client = JobdClient(base_url="http://broker.test")

    out = jobd_list(client, {"state": ["queued"], "limit": 10_000})
    assert [c.request.url.params["limit"] for c in route.calls] == ["200"]
    assert len(out["jobs"]) == 200 and out["counts"] == {"queued": 300}, out["counts"]
    assert out["truncated"] == 100

    # Positive control: an in-range limit goes through unchanged.
    before = len(route.calls)
    out = jobd_list(client, {"state": ["queued"], "limit": 7})
    assert [c.request.url.params["limit"] for c in list(route.calls)[before:]] == ["7"]
    assert len(out["jobs"]) == 7
