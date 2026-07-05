from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
import respx

from jobd.client import JobdClient
from jobd.mcp.tools import jobd_submit


@respx.mock
def test_submit_async_returns_job_id_and_state():
    respx.post("http://broker.test/submit").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": 7,
                "state": "queued",
                "project": "p",
                "host_pin": "any",
                "queued_at": "2026-04-26T00:00:00Z",
            },
        )
    )
    client = JobdClient(base_url="http://broker.test")
    out = jobd_submit(client, {"command": "x", "project": "p", "cwd": "/x"})
    assert out["job_id"] == 7
    assert out["state"] == "queued"
    assert "warning" not in out  # no warning by default


@respx.mock
def test_submit_async_surfaces_broker_warning():
    respx.post("http://broker.test/submit").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": 8,
                "state": "queued",
                "project": "p",
                "host_pin": "laptop",
                "queued_at": "t",
                "warning": "no laptop worker has heartbeat in 24h",
            },
        )
    )
    client = JobdClient(base_url="http://broker.test")
    out = jobd_submit(client, {"command": "x", "project": "p", "cwd": "/x", "host": "laptop"})
    assert out["warning"] == "no laptop worker has heartbeat in 24h"


@respx.mock
def test_submit_array_returns_broker_summary_as_is():
    """count>1: broker returns {array_id, count, job_ids, warnings}, which is
    NOT a JobInfo. jobd_submit must return it unchanged (no xlate_job_info,
    which would KeyError on the missing id/worker fields)."""
    summary = {"array_id": 12, "count": 3, "job_ids": [12, 13, 14], "warnings": []}
    respx.post("http://broker.test/submit").mock(return_value=httpx.Response(200, json=summary))
    client = JobdClient(base_url="http://broker.test")
    out = jobd_submit(
        client, {"command": "echo {i}", "project": "p", "cwd": "/x", "extra": {"count": 3}}
    )
    assert out == summary


@respx.mock
def test_submit_array_wait_aggregates_when_all_complete():
    """wait=True on an array polls every member to terminal and returns an
    aggregate, not the bare summary."""
    summary = {"array_id": 12, "count": 3, "job_ids": [12, 13, 14], "warnings": []}
    respx.post("http://broker.test/submit").mock(return_value=httpx.Response(200, json=summary))
    for jid in (12, 13, 14):
        respx.get(f"http://broker.test/jobs/{jid}").mock(
            return_value=httpx.Response(
                200, json={"job_id": jid, "state": "completed", "exit_code": 0}
            )
        )
    client = JobdClient(base_url="http://broker.test")
    out = jobd_submit(
        client,
        {
            "command": "echo {i}",
            "project": "p",
            "cwd": "/x",
            "wait": True,
            "extra": {"count": 3},
        },
    )
    assert out["array_id"] == 12
    assert out["all_completed"] is True
    assert out["states"] == {"completed": 3}
    assert [m["job_id"] for m in out["members"]] == [12, 13, 14]
    assert all(m["exit_code"] == 0 for m in out["members"])
    assert "timed_out" not in out


@respx.mock
def test_submit_array_wait_reports_timeout_with_partial_progress():
    """If a member is still running when the shared deadline elapses, the
    aggregate carries timed_out + the last-known per-member states."""
    summary = {"array_id": 20, "count": 2, "job_ids": [20, 21], "warnings": []}
    respx.post("http://broker.test/submit").mock(return_value=httpx.Response(200, json=summary))
    respx.get("http://broker.test/jobs/20").mock(
        return_value=httpx.Response(200, json={"job_id": 20, "state": "completed", "exit_code": 0})
    )
    respx.get("http://broker.test/jobs/21").mock(
        return_value=httpx.Response(200, json={"job_id": 21, "state": "running", "exit_code": None})
    )
    client = JobdClient(base_url="http://broker.test")
    times = iter([0.0, 11.0, 11.0, 11.0])
    with (
        patch("jobd.mcp.tools.time.monotonic", side_effect=lambda: next(times)),
        patch("jobd.mcp.tools.time.sleep"),
    ):
        out = jobd_submit(
            client,
            {
                "command": "echo {i}",
                "project": "p",
                "cwd": "/x",
                "wait": True,
                "wait_timeout_s": 10,
                "extra": {"count": 2},
            },
        )
    assert out["timed_out"] is True
    assert out["all_completed"] is False
    assert out["states"] == {"completed": 1, "running": 1}
    assert "hint" in out


@respx.mock
def test_submit_extra_keys_translate_to_broker_payload():
    """Translation: idempotent → requires.idempotent; depends_on top-level;
    max_wall dropped (broker has no field). cmd wrapped with bash -c.
    """
    import json as _json

    route = respx.post("http://broker.test/submit").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 9,
                "state": "queued",
                "project": "p",
                "host_pin": "any",
                "submitted_at": "2026-04-26T00:00:00+00:00",
            },
        )
    )
    client = JobdClient(base_url="http://broker.test")
    jobd_submit(
        client,
        {
            "command": "x",
            "project": "p",
            "cwd": "/x",
            "extra": {"idempotent": True, "depends_on": [1, 2], "max_wall": "1h"},
        },
    )
    body = _json.loads(route.calls.last.request.content)
    assert body["cmd"] == ["bash", "-c", "x"]
    assert body["depends_on"] == [1, 2]
    assert body.get("requires", {}).get("idempotent") is True
    assert "max_wall" not in body
    assert "idempotent" not in body  # nested, not top-level


@respx.mock
def test_submit_stamps_submitted_via_mcp():
    """#51: every jobd_submit call must stamp submitted_via='mcp' on the
    broker payload. Without this, MCP submissions are indistinguishable from
    CLI submissions in the broker DB."""
    import json as _json

    route = respx.post("http://broker.test/submit").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 1,
                "state": "queued",
                "project": "p",
                "host_pin": "any",
                "submitted_at": "2026-04-29T00:00:00+00:00",
            },
        )
    )
    client = JobdClient(base_url="http://broker.test")
    jobd_submit(client, {"command": "x", "project": "p", "cwd": "/x"})
    body = _json.loads(route.calls.last.request.content)
    assert body["submitted_via"] == "mcp"


@respx.mock
def test_submit_dry_run_returns_plan_unchanged():
    """T22: dry_run=true on the MCP surface threads through to the broker
    /submit dry-run branch and the plan response is returned as-is — no
    xlate_job_info (which expects JobInfo with `id`)."""
    import json as _json

    route = respx.post("http://broker.test/submit").mock(
        return_value=httpx.Response(
            200,
            json={
                "state": "dry-run",
                "would_route_to": ["laptop", "desktop"],
                "would_use_worker": None,
                "validation": {
                    "effective_priority": 55,
                    "effective_host_pin": "any",
                    "effective_preemptible": False,
                    "warnings": [],
                },
            },
        )
    )
    client = JobdClient(base_url="http://broker.test")
    out = jobd_submit(client, {"command": "x", "project": "p", "cwd": "/x", "dry_run": True})
    body = _json.loads(route.calls.last.request.content)
    assert body["dry_run"] is True
    assert out["state"] == "dry-run"
    assert out["would_route_to"] == ["laptop", "desktop"]
    assert out["would_use_worker"] is None
    assert out["validation"]["effective_priority"] == 55
    # job_id absent — dry-run never queues, so no id is fabricated.
    assert "job_id" not in out


@respx.mock
def test_jobd_status_async_returns_full_info():
    respx.get("http://broker.test/jobs/7").mock(
        return_value=httpx.Response(
            200, json={"job_id": 7, "state": "running", "exit_code": None, "host": "desktop"}
        )
    )
    from jobd.mcp.tools import jobd_status

    client = JobdClient(base_url="http://broker.test")
    out = jobd_status(client, {"job_id": 7})
    assert out["state"] == "running"
    assert out["host"] == "desktop"


@respx.mock
def test_jobd_status_wait_returns_timeout_when_running():
    respx.get("http://broker.test/jobs/7").mock(
        return_value=httpx.Response(200, json={"job_id": 7, "state": "running", "exit_code": None})
    )
    from jobd.mcp.tools import jobd_status

    client = JobdClient(base_url="http://broker.test")
    times = iter([0.0, 11.0])
    with (
        patch("jobd.mcp.tools.time.monotonic", side_effect=lambda: next(times)),
        patch("jobd.mcp.tools.time.sleep"),
    ):
        out = jobd_status(client, {"job_id": 7, "wait": True, "wait_timeout_s": 10})
    assert out["timed_out"] is True


@respx.mock
def test_jobd_logs_passes_tail_bytes_through():
    route = respx.get("http://broker.test/jobs/7/output").mock(
        return_value=httpx.Response(
            200,
            json={
                "tail": "abc",
                "size_bytes": 3,
                "returned_bytes": 3,
                "truncated": False,
                "has_log": True,
            },
        )
    )
    from jobd.mcp.tools import jobd_logs

    client = JobdClient(base_url="http://broker.test")
    out = jobd_logs(client, {"job_id": 7, "tail_bytes": 1000})
    assert out["log_tail"] == "abc"
    assert "tail" not in out
    assert route.calls.last.request.url.params["tail"] == "1000"


@respx.mock
def test_jobd_cancel_returns_prior_and_new_state():
    respx.get("http://broker.test/jobs/7").mock(
        side_effect=[
            httpx.Response(200, json={"job_id": 7, "state": "running"}),  # prior
            httpx.Response(
                200, json={"job_id": 7, "state": "running", "signal": "cancel"}
            ),  # post-cancel
        ]
    )
    respx.post("http://broker.test/jobs/7/cancel").mock(
        return_value=httpx.Response(200, json={"job_id": 7, "state": "running", "signal": "cancel"})
    )
    from jobd.mcp.tools import jobd_cancel

    client = JobdClient(base_url="http://broker.test")
    out = jobd_cancel(client, {"job_id": 7, "reason": "test"})
    assert out["prior_state"] == "running"
    assert out["new_state"] == "running"
    assert out["signal_sent"] == "cancel"


@respx.mock
def test_jobd_cancel_signal_sent_when_prior_state_assigned():
    """Cancel of an `assigned` job (worker has claimed but not yet POSTed
    /started) must still report signal_sent='cancel' — the broker queues
    SIGTERM identically for assigned and running."""
    respx.get("http://broker.test/jobs/7").mock(
        side_effect=[
            httpx.Response(200, json={"job_id": 7, "state": "assigned"}),  # prior
            httpx.Response(200, json={"job_id": 7, "state": "assigned", "signal": "cancel"}),
        ]
    )
    respx.post("http://broker.test/jobs/7/cancel").mock(
        return_value=httpx.Response(
            200, json={"job_id": 7, "state": "assigned", "signal": "cancel"}
        )
    )
    from jobd.mcp.tools import jobd_cancel

    client = JobdClient(base_url="http://broker.test")
    out = jobd_cancel(client, {"job_id": 7})
    assert out["prior_state"] == "assigned"
    assert out["signal_sent"] == "cancel"


@respx.mock
def test_jobd_cancel_signal_sent_null_when_prior_queued():
    """Cancel of a `queued` job is a state flip, not a SIGTERM — signal_sent
    must remain None to distinguish from cancel-of-in-flight."""
    respx.get("http://broker.test/jobs/7").mock(
        side_effect=[
            httpx.Response(200, json={"job_id": 7, "state": "queued"}),
            httpx.Response(200, json={"job_id": 7, "state": "cancelled"}),
        ]
    )
    respx.post("http://broker.test/jobs/7/cancel").mock(
        return_value=httpx.Response(200, json={"job_id": 7, "state": "cancelled"})
    )
    from jobd.mcp.tools import jobd_cancel

    client = JobdClient(base_url="http://broker.test")
    out = jobd_cancel(client, {"job_id": 7})
    assert out["prior_state"] == "queued"
    assert out["signal_sent"] is None


@respx.mock
def test_jobd_preempt_returns_prior_and_new_state():
    """Successful preempt: signal_sent='preempt', prior+new state echoed."""
    respx.get("http://broker.test/jobs/9").mock(
        side_effect=[
            httpx.Response(200, json={"job_id": 9, "state": "running"}),
            httpx.Response(200, json={"job_id": 9, "state": "running", "signal": "preempt"}),
        ]
    )
    respx.post("http://broker.test/jobs/9/preempt").mock(
        return_value=httpx.Response(
            200, json={"job_id": 9, "state": "running", "signal": "preempt"}
        )
    )
    from jobd.mcp.tools import jobd_preempt

    client = JobdClient(base_url="http://broker.test")
    out = jobd_preempt(client, {"job_id": 9})
    assert out["prior_state"] == "running"
    assert out["new_state"] == "running"
    assert out["signal_sent"] == "preempt"


@respx.mock
def test_jobd_preempt_409_when_not_preemptible_dispatches_as_error():
    """Broker 409 (job not preemptible / not running) bubbles through the
    BrokerRefusal layer; the tool itself doesn't swallow it."""
    respx.get("http://broker.test/jobs/9").mock(
        return_value=httpx.Response(200, json={"job_id": 9, "state": "running"})
    )
    respx.post("http://broker.test/jobs/9/preempt").mock(
        return_value=httpx.Response(409, json={"detail": "job 9 is not preemptible"})
    )
    from jobd.client import BrokerRefusal
    from jobd.mcp.tools import jobd_preempt

    client = JobdClient(base_url="http://broker.test")
    try:
        jobd_preempt(client, {"job_id": 9})
    except BrokerRefusal as e:
        assert e.status_code == 409
        assert "not preemptible" in (e.detail or "")
    else:
        raise AssertionError("expected BrokerRefusal")


@respx.mock
def test_jobd_list_summarizes_jobs():
    """Live broker returns bare list[JobInfo] with `id`/`worker`/`submitted_at`.
    Translation renames to job_id/host/queued_at and counts derive client-side.
    """
    respx.get("http://broker.test/jobs").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "project": "p",
                    "state": "queued",
                    "worker": None,
                    "exit_code": None,
                    "submitted_at": "2026-04-26T00:00:00+00:00",
                    "started_at": None,
                    "extra_field_dropped": "x",
                },
                {
                    "id": 2,
                    "project": "p",
                    "state": "running",
                    "worker": "desktop",
                    "exit_code": None,
                    "submitted_at": "2026-04-26T00:00:00+00:00",
                    "started_at": "2026-04-26T00:00:01+00:00",
                },
            ],
        )
    )
    from jobd.mcp.tools import jobd_list

    client = JobdClient(base_url="http://broker.test")
    out = jobd_list(client, {"state": ["queued", "running"]})
    assert out["counts"]["queued"] == 1
    assert out["counts"]["running"] == 1
    assert len(out["jobs"]) == 2
    assert "extra_field_dropped" not in out["jobs"][0]
    assert set(out["jobs"][0].keys()) == {
        "job_id",
        "project",
        "state",
        "host",
        "exit_code",
        "queued_at",
        "started_at",
    }
    assert out["jobs"][0]["job_id"] == 1
    assert out["jobs"][1]["host"] == "desktop"


def _job_row(i: int, state: str) -> dict:
    return {
        "id": i,
        "project": "p",
        "state": state,
        "worker": None,
        "exit_code": None,
        "submitted_at": "2026-04-26T00:00:00+00:00",
        "started_at": None,
    }


@respx.mock
def test_jobd_list_defaults_to_active_states():
    """A6 (audit 2026-07-05): with `state` omitted, jobd_list must apply the
    documented active-set default instead of dumping the broker's full job
    history (retention is off by default) into the model's context."""
    respx.get("http://broker.test/jobs").mock(
        return_value=httpx.Response(
            200,
            json=[
                _job_row(4, "running"),
                _job_row(3, "completed"),
                _job_row(2, "failed"),
                _job_row(1, "queued"),
            ],
        )
    )
    from jobd.mcp.tools import jobd_list

    client = JobdClient(base_url="http://broker.test")
    out = jobd_list(client, {})
    assert {j["state"] for j in out["jobs"]} == {"running", "queued"}
    assert out["counts"] == {"running": 1, "queued": 1}

    # Explicit [] opts into all states.
    out_all = jobd_list(client, {"state": []})
    assert len(out_all["jobs"]) == 4


@respx.mock
def test_jobd_list_applies_limit_and_reports_truncation():
    """A6: `limit` was advertised in the schema but never applied anywhere.
    It now caps the jobs array (newest first — broker order), while counts
    still cover the full filtered set and `truncated` reports the overflow."""
    respx.get("http://broker.test/jobs").mock(
        return_value=httpx.Response(200, json=[_job_row(i, "queued") for i in range(10, 0, -1)])
    )
    from jobd.mcp.tools import jobd_list

    client = JobdClient(base_url="http://broker.test")
    out = jobd_list(client, {"state": ["queued"], "limit": 3})
    assert [j["job_id"] for j in out["jobs"]] == [10, 9, 8]  # newest kept
    assert out["counts"] == {"queued": 10}
    assert out["truncated"] == 7


@respx.mock
def test_jobd_list_forwards_single_state_to_broker():
    """One state = broker-side filter (state_filter param); multiple states
    fetch unfiltered and filter client-side."""
    route = respx.get("http://broker.test/jobs").mock(
        return_value=httpx.Response(200, json=[_job_row(1, "queued")])
    )
    from jobd.mcp.tools import jobd_list

    client = JobdClient(base_url="http://broker.test")
    jobd_list(client, {"state": ["queued"]})
    assert route.calls[0].request.url.params.get("state_filter") == "queued"

    jobd_list(client, {"state": ["queued", "running"]})
    assert "state_filter" not in route.calls[1].request.url.params


@respx.mock
def test_jobd_workers_healthy_when_recent_heartbeat():
    recent = datetime.now(UTC).isoformat()
    respx.get("http://broker.test/workers").mock(
        return_value=httpx.Response(
            200, json={"workers": [{"host": "desktop", "last_heartbeat": recent}]}
        )
    )
    from jobd.mcp.tools import jobd_workers

    client = JobdClient(base_url="http://broker.test")
    out = jobd_workers(client, {})
    assert out["fleet_health"] == "healthy"
    assert out["warnings"] == []


@respx.mock
def test_jobd_workers_degraded_when_stale():
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    respx.get("http://broker.test/workers").mock(
        return_value=httpx.Response(
            200, json={"workers": [{"host": "desktop", "last_heartbeat": old}]}
        )
    )
    from jobd.mcp.tools import jobd_workers

    client = JobdClient(base_url="http://broker.test")
    out = jobd_workers(client, {})
    assert out["fleet_health"] == "degraded"
    assert any("stale" in w for w in out["warnings"])


@respx.mock
def test_jobd_workers_empty_fleet():
    respx.get("http://broker.test/workers").mock(
        return_value=httpx.Response(200, json={"workers": []})
    )
    from jobd.mcp.tools import jobd_workers

    client = JobdClient(base_url="http://broker.test")
    out = jobd_workers(client, {})
    assert out["fleet_health"] == "empty"


@respx.mock
def test_jobd_job_get_returns_full_info():
    respx.get("http://broker.test/jobs/7").mock(
        return_value=httpx.Response(
            200, json={"job_id": 7, "command": "x", "depends_on": [3], "fast_path": True}
        )
    )
    from jobd.mcp.tools import jobd_job_get

    client = JobdClient(base_url="http://broker.test")
    out = jobd_job_get(client, {"job_id": 7})
    assert out["job_id"] == 7
    assert out["fast_path"] is True
    assert out["depends_on"] == [3]


@respx.mock
def test_jobd_worker_delete_success():
    respx.delete("http://broker.test/workers/ghost").mock(
        return_value=httpx.Response(200, json={"ok": True, "deleted": "ghost"})
    )
    from jobd.mcp.tools import jobd_worker_delete

    client = JobdClient(base_url="http://broker.test")
    out = jobd_worker_delete(client, {"host": "ghost"})
    assert out == {"ok": True, "deleted": "ghost"}


@respx.mock
def test_jobd_worker_delete_409_when_online_dispatches_as_error():
    respx.delete("http://broker.test/workers/laptop").mock(
        return_value=httpx.Response(409, json={"detail": "worker 'laptop' is online"})
    )
    from jobd.mcp.server import build_server

    client = JobdClient(base_url="http://broker.test")
    server = build_server(client=client)
    out = server._jobd_dispatch(  # type: ignore[attr-defined]
        "jobd_worker_delete", {"host": "laptop"}
    )
    assert "error" in out


@respx.mock
def test_dispatch_maps_broker_refusal_to_structured_error():
    respx.post("http://broker.test/submit").mock(
        return_value=httpx.Response(400, json={"detail": "cwd /mnt/c/foo is under /mnt/c/"})
    )
    from jobd.mcp.server import build_server

    client = JobdClient(base_url="http://broker.test")
    server = build_server(client=client)
    out = server._jobd_dispatch(  # type: ignore[attr-defined]
        "jobd_submit", {"command": "x", "project": "p", "cwd": "/mnt/c/foo"}
    )
    assert "error" in out
    assert out["error"]["kind"] == "cwd_outside_mount_roots"
    assert "host" in out["error"]["hint"].lower() or "mount" in out["error"]["hint"].lower()


@respx.mock
def test_dispatch_passes_through_success_payload():
    respx.get("http://broker.test/jobs/7").mock(
        return_value=httpx.Response(200, json={"job_id": 7, "state": "running"})
    )
    from jobd.mcp.server import build_server

    client = JobdClient(base_url="http://broker.test")
    server = build_server(client=client)
    out = server._jobd_dispatch("jobd_status", {"job_id": 7})  # type: ignore[attr-defined]
    assert out["job_id"] == 7
    assert out["state"] == "running"


@respx.mock
def test_call_tool_writes_jsonl_log_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBD_MCP_LOG_DIR", str(tmp_path))
    respx.get("http://broker.test/jobs/7").mock(
        return_value=httpx.Response(200, json={"job_id": 7, "state": "completed"})
    )
    import json as _json
    import time as _time

    from jobd.mcp.server import _log_call

    t0 = _time.monotonic()
    _log_call("jobd_status", {"job_id": 7}, None, (_time.monotonic() - t0) * 1000)

    log_file = tmp_path / "calls.jsonl"
    assert log_file.exists()
    line = log_file.read_text().strip().splitlines()[-1]
    entry = _json.loads(line)
    assert entry["tool"] == "jobd_status"
    assert entry["job_id"] == 7
    assert "ms" in entry
    assert "error_kind" not in entry


def test_call_tool_logs_error_kind_for_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBD_MCP_LOG_DIR", str(tmp_path))
    import json as _json

    from jobd.mcp.server import _log_call

    _log_call("jobd_submit", {"command": "x"}, "cwd_outside_mount_roots", 12.3)
    entry = _json.loads((tmp_path / "calls.jsonl").read_text().strip().splitlines()[-1])
    assert entry["error_kind"] == "cwd_outside_mount_roots"
    assert entry["tool"] == "jobd_submit"
