"""Regression tests for the 2026-09-22 jobd-mcp audit (H1, H2, M3-M6, L7-L12,
plus httpx WriteTimeout/PoolTimeout).

Every test pairs the failure it guards with a positive control inside the same
test, so a harness that rejects everything (or accepts everything) cannot pass.
Where the claim is about what an agent sees, the call goes over the real MCP
protocol (`mcp.client.Client`), not the `_jobd_dispatch` escape hatch. Where the
claim is about what the broker receives, the broker is the real FastAPI app
behind `TestClient`, not a respx stub.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from mcp.client import Client

from jobd.client import BrokerUnreachable, JobdClient
from jobd.mcp.server import build_server
from jobd.mcp.translate import xlate_submit_payload

BROKER = "http://broker.test"


def _call(server, name: str, args: dict) -> tuple[bool, dict | str]:
    """One tool call over the real MCP protocol -> (is_error, parsed payload)."""

    async def go():
        async with Client(server) as cl:
            return await cl.call_tool(name, args)

    r = asyncio.run(go())
    text = r.content[0].text
    try:
        return bool(r.is_error), json.loads(text)
    except json.JSONDecodeError:
        return bool(r.is_error), text


def _real_broker_client(app) -> JobdClient:
    """JobdClient wired to the real in-process broker app."""
    c = JobdClient(base_url="http://testserver")
    c._client = TestClient(app)
    return c


def _hb(tc: TestClient, host: str, *, gpu: bool = False) -> None:
    body = {
        "host": host,
        "free_vram_gb": 24 if gpu else 0,
        "unregistered_vram_gb": 0,
        "free_ram_gb": 8,
        "idle_cpus": 4,
        "gpu": gpu,
        "mount_roots": ["/tmp"],
    }
    r = tc.post("/heartbeat", json=body)
    assert r.status_code == 200, r.text


# --- H1: gpu:false must mean "don't care", like the CLI's absent --gpu --------


def test_h1_gpu_false_does_not_forbid_gpu_workers():
    no_pref = xlate_submit_payload({"command": "x", "project": "p", "cwd": "/x", "gpu": False})
    assert (no_pref.get("requires") or {}).get("gpu") is None, no_pref
    absent = xlate_submit_payload({"command": "x", "project": "p", "cwd": "/x"})
    assert (absent.get("requires") or {}).get("gpu") is None, absent
    # positive control: gpu:true still pins to a GPU worker
    pinned = xlate_submit_payload({"command": "x", "project": "p", "cwd": "/x", "gpu": True})
    assert pinned["requires"]["gpu"] is True


def test_h1_gpu_false_job_is_claimable_by_an_all_gpu_fleet(app):
    """The audit's live shape: every worker is gpu:true. An explicit gpu:false
    submit must still be dispatchable there."""
    c = _real_broker_client(app)
    tc = c._client
    _hb(tc, "gpubox", gpu=True)
    server = build_server(client=c)
    is_err, out = _call(
        server, "jobd_submit", {"command": "true", "project": "p", "cwd": "/tmp", "gpu": False}
    )
    assert not is_err, out
    job = tc.get(f"/jobs/{out['job_id']}").json()
    assert (job.get("requires") or {}).get("gpu") is None, job["requires"]
    claim = tc.post(
        "/next-job",
        json={
            "host": "gpubox",
            "free_vram_gb": 24,
            "unregistered_vram_gb": 0,
            "free_ram_gb": 8,
            "idle_cpus": 4,
            "gpu": True,
            "mount_roots": ["/tmp"],
        },
    )
    assert claim.status_code == 200, claim.text
    assert claim.json() and claim.json()["id"] == out["job_id"]


# --- H2 / M3 / L9 / M6: arguments are validated against the advertised schema --


def _err_kind(out) -> str | None:
    return out.get("error", {}).get("kind") if isinstance(out, dict) else None


@pytest.mark.parametrize("key,val", [("depends_on", [1]), ("priority", 5), ("max_wall_s", 60)])
@respx.mock
def test_h2_top_level_extra_keys_are_rejected_not_dropped(key, val):
    route = respx.post(f"{BROKER}/submit").mock(
        return_value=httpx.Response(
            200, json={"id": 1, "state": "queued", "project": "p", "host_pin": "any"}
        )
    )
    server = build_server(client=JobdClient(base_url=BROKER))
    base = {"command": "x", "project": "p", "cwd": "/x"}
    is_err, out = _call(server, "jobd_submit", {**base, key: val})
    assert is_err and _err_kind(out) == "invalid_arguments", out
    assert key in out["error"]["message"]
    assert "extra" in out["error"]["hint"], out  # tells the agent where it belongs
    assert not route.called, "a rejected call must not reach the broker"
    # positive control: the same key under `extra` reaches the broker body
    is_err, out = _call(server, "jobd_submit", {**base, "extra": {key: val}})
    assert not is_err, out
    sent = json.loads(route.calls.last.request.content)
    broker_key = "priority_delta" if key == "priority" else key
    assert sent[broker_key] == val, sent


@respx.mock
def test_m3_typo_in_extra_is_rejected():
    route = respx.post(f"{BROKER}/submit").mock(
        return_value=httpx.Response(
            200, json={"id": 1, "state": "queued", "project": "p", "host_pin": "any"}
        )
    )
    server = build_server(client=JobdClient(base_url=BROKER))
    base = {"command": "x", "project": "p", "cwd": "/x"}
    is_err, out = _call(server, "jobd_submit", {**base, "extra": {"max_wal_s": 60}})
    assert is_err and _err_kind(out) == "invalid_arguments", out
    assert "max_wal_s" in out["error"]["message"]
    assert not route.called
    # positive control
    is_err, out = _call(server, "jobd_submit", {**base, "extra": {"max_wall_s": 60}})
    assert not is_err, out
    assert json.loads(route.calls.last.request.content)["max_wall_s"] == 60


@respx.mock
def test_l9_string_where_list_expected_is_rejected_not_split():
    route = respx.post(f"{BROKER}/submit").mock(
        return_value=httpx.Response(
            200, json={"id": 1, "state": "queued", "project": "p", "host_pin": "any"}
        )
    )
    respx.get(f"{BROKER}/jobs").mock(
        return_value=httpx.Response(200, json=[], headers={"X-Total-Count": "0"})
    )
    server = build_server(client=JobdClient(base_url=BROKER))
    base = {"command": "x", "project": "p", "cwd": "/x"}
    is_err, out = _call(server, "jobd_submit", {**base, "needs": "cuda"})
    assert is_err and _err_kind(out) == "invalid_arguments", out
    assert not route.called
    is_err, out = _call(server, "jobd_list", {"state": "running"})
    assert is_err and _err_kind(out) == "invalid_arguments", out
    # positive controls
    is_err, out = _call(server, "jobd_submit", {**base, "needs": ["cuda"]})
    assert not is_err, out
    assert json.loads(route.calls.last.request.content)["requires"]["needs"] == ["cuda"]
    is_err, out = _call(server, "jobd_list", {"state": ["running"]})
    assert not is_err and out["counts"] == {"running": 0}, out


@respx.mock
def test_m6_unknown_state_name_is_rejected_by_mcp():
    respx.get(f"{BROKER}/jobs").mock(
        return_value=httpx.Response(200, json=[], headers={"X-Total-Count": "0"})
    )
    server = build_server(client=JobdClient(base_url=BROKER))
    is_err, out = _call(server, "jobd_list", {"state": ["runing"]})
    assert is_err and _err_kind(out) == "invalid_arguments", out
    assert "runing" in out["error"]["message"]
    is_err, out = _call(server, "jobd_list", {"state": ["running", "failed"]})
    assert not is_err, out


def test_m6_unknown_state_filter_is_rejected_by_broker(client):
    """Root of M6: the broker compared the filter as a free string, so a typo
    matched nothing and returned an honest-looking empty page to EVERY client."""
    assert client.get("/jobs", params={"state_filter": "runing"}).status_code == 422
    r = client.get("/jobs", params={"state_filter": "running"})
    assert r.status_code == 200 and r.json() == []


@respx.mock
def test_unknown_top_level_argument_rejected_on_every_tool():
    respx.get(f"{BROKER}/jobs/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "state": "running"})
    )
    server = build_server(client=JobdClient(base_url=BROKER))
    is_err, out = _call(server, "jobd_status", {"job_id": 7, "wiat": True})
    assert is_err and _err_kind(out) == "invalid_arguments", out
    is_err, out = _call(server, "jobd_status", {"job_id": 7})
    assert not is_err and out["job_id"] == 7


# --- M4: wait=true keeps the async fields and reports log truncation -----------


@respx.mock
def test_m4_submit_wait_keeps_warning_label_and_truncation():
    respx.post(f"{BROKER}/submit").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 9,
                "state": "completed",
                "project": "proj",
                "project_label": "Proj",
                "host_pin": "any",
                "warning": "routed to a stale worker",
            },
        )
    )
    respx.get(f"{BROKER}/jobs/9").mock(
        return_value=httpx.Response(200, json={"id": 9, "state": "completed", "exit_code": 0})
    )
    respx.get(f"{BROKER}/jobs/9/output").mock(
        return_value=httpx.Response(
            200,
            json={
                "tail": "x" * 8192,
                "size_bytes": 50000,
                "returned_bytes": 8192,
                "truncated": True,
            },
        )
    )
    out = __import__("jobd.mcp.tools", fromlist=["jobd_submit"]).jobd_submit(
        JobdClient(base_url=BROKER),
        {"command": "x", "project": "Proj", "cwd": "/x", "wait": True},
    )
    assert out["warning"] == "routed to a stale worker", out
    assert out["project_label"] == "Proj"
    assert out["log_truncated"] is True and out["log_size_bytes"] == 50000, out
    # positive control: the terminal fields are still there
    assert out["state"] == "completed" and out["exit_code"] == 0
    assert out["log_tail"] == "x" * 8192


# --- M5 + WriteTimeout/PoolTimeout: every transport failure is classified ------


@pytest.mark.parametrize(
    "exc",
    [
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
        httpx.WriteTimeout("write timed out"),
        httpx.PoolTimeout("pool timed out"),
    ],
    ids=["RemoteProtocolError", "WriteTimeout", "PoolTimeout"],
)
@respx.mock
def test_m5_every_httpx_transport_error_is_a_transport_error(exc, tmp_path, monkeypatch):
    monkeypatch.setenv("JOBD_MCP_LOG_DIR", str(tmp_path))
    respx.get(f"{BROKER}/jobs/7").mock(side_effect=exc)
    c = JobdClient(base_url=BROKER)
    with pytest.raises(BrokerUnreachable):
        c.status(7)
    is_err, out = _call(build_server(client=c), "jobd_status", {"job_id": 7})
    assert is_err, out
    assert isinstance(out, str) and out.startswith("jobd transport error"), out
    logged = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert logged[-1]["error_kind"].startswith("transport"), logged[-1]
    # positive control: a healthy broker still answers
    respx.get(f"{BROKER}/jobs/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "state": "running"})
    )
    assert c.status(7)["state"] == "running"


# --- L8: failure classification is by origin, not by builtin exception type ----


@respx.mock
def test_l8_non_json_broker_reply_is_not_labelled_unknown_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBD_MCP_LOG_DIR", str(tmp_path))
    respx.get(f"{BROKER}/jobs/7").mock(return_value=httpx.Response(200, text="<html>proxy</html>"))
    server = build_server(client=JobdClient(base_url=BROKER))
    is_err, out = _call(server, "jobd_status", {"job_id": 7})
    assert is_err, out
    logged = json.loads((tmp_path / "calls.jsonl").read_text().splitlines()[-1])
    assert logged.get("error_kind") not in (None, "unknown_tool"), logged
    # positive control: a genuinely unknown tool is still unknown_tool
    is_err, out = _call(server, "jobd_nope", {})
    assert is_err
    logged = json.loads((tmp_path / "calls.jsonl").read_text().splitlines()[-1])
    assert logged["error_kind"] == "unknown_tool", logged


@respx.mock
def test_l8_bad_argument_value_is_invalid_arguments_not_unknown_tool():
    respx.get(f"{BROKER}/jobs/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "state": "completed"})
    )
    server = build_server(client=JobdClient(base_url=BROKER))
    is_err, out = _call(server, "jobd_status", {"job_id": 7, "wait": True, "wait_timeout_s": "abc"})
    assert is_err and _err_kind(out) == "invalid_arguments", out
    is_err, out = _call(server, "jobd_status", {"job_id": 7, "wait": True, "wait_timeout_s": 5})
    assert not is_err and out["state"] == "completed", out


# --- L7: cancel reason reaches the broker and is recorded ----------------------


def test_l7_cancel_reason_is_recorded_in_the_cancel_event(app, tmp_path):
    c = _real_broker_client(app)
    tc = c._client
    server = build_server(client=c)

    def submit() -> int:
        r = tc.post("/submit", json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"})
        assert r.status_code == 200, r.text
        return r.json()["id"]

    j1, j2 = submit(), submit()
    is_err, out = _call(server, "jobd_cancel", {"job_id": j1, "reason": "wrong seed"})
    assert not is_err and out["new_state"] == "cancelled", out
    # positive control: no reason is still a valid cancel
    is_err, out = _call(server, "jobd_cancel", {"job_id": j2})
    assert not is_err and out["new_state"] == "cancelled", out

    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
        if '"job_cancelled"' in line
    ]
    by_job = {e["job_id"]: e["payload"] for e in events}
    assert by_job[j1].get("reason") == "wrong seed", by_job
    assert "reason" not in by_job[j2] or by_job[j2]["reason"] is None, by_job


# --- L10: path segments are URL-encoded ----------------------------------------


@respx.mock
def test_l10_worker_delete_encodes_host():
    hashed = respx.delete(f"{BROKER}/workers/gt76%23x").mock(
        return_value=httpx.Response(200, json={"ok": True, "deleted": "gt76#x"})
    )
    bare = respx.delete(f"{BROKER}/workers/gt76").mock(
        return_value=httpx.Response(200, json={"ok": True, "deleted": "gt76"})
    )
    c = JobdClient(base_url=BROKER)
    assert c.delete_worker("gt76#x")["deleted"] == "gt76#x"
    assert hashed.called and not bare.called, "fragment char truncated the path to /workers/gt76"
    # positive control
    assert c.delete_worker("gt76")["deleted"] == "gt76"
    assert bare.called


def test_l10_worker_delete_encoded_host_against_real_broker(app):
    c = _real_broker_client(app)
    tc = c._client
    _hb(tc, "gt76")
    tc.app  # noqa: B018 — keep the app alive
    # gt76 is online, so a delete that reached it would 409; the '#x' host does
    # not exist, so the correct answer is 404 naming the whole host.
    from jobd.client import BrokerRefusal

    with pytest.raises(BrokerRefusal) as ei:
        c.delete_worker("gt76#x")
    assert ei.value.status_code == 404 and "gt76#x" in ei.value.detail, ei.value.detail


# --- L11: signal_sent reflects what the broker did, not the pre-read state -----


@respx.mock
def test_l11_signal_sent_comes_from_the_cancel_response():
    from jobd.mcp.tools import jobd_cancel

    # prior read says running, but the job finished before the cancel landed:
    # the broker's cancel reply shows it terminal with no signal queued.
    respx.get(f"{BROKER}/jobs/5").mock(
        return_value=httpx.Response(200, json={"id": 5, "state": "running"})
    )
    respx.post(f"{BROKER}/jobs/5/cancel").mock(
        return_value=httpx.Response(200, json={"id": 5, "state": "completed"})
    )
    out = jobd_cancel(JobdClient(base_url=BROKER), {"job_id": 5})
    assert out["signal_sent"] is None, out
    assert out["new_state"] == "completed"
    # positive control: the broker left it running => a cancel signal is pending
    respx.post(f"{BROKER}/jobs/5/cancel").mock(
        return_value=httpx.Response(200, json={"id": 5, "state": "running"})
    )
    out = jobd_cancel(JobdClient(base_url=BROKER), {"job_id": 5})
    assert out["signal_sent"] == "cancel", out


# --- L12: error hints name the resource the tool acts on ----------------------


@respx.mock
def test_l12_worker_delete_hints_talk_about_workers():
    respx.delete(f"{BROKER}/workers/ghost").mock(
        return_value=httpx.Response(404, json={"detail": "no such worker: ghost"})
    )
    respx.delete(f"{BROKER}/workers/laptop").mock(
        return_value=httpx.Response(409, json={"detail": "worker 'laptop' is online"})
    )
    respx.get(f"{BROKER}/jobs/99").mock(
        return_value=httpx.Response(404, json={"detail": "no such job: 99"})
    )
    server = build_server(client=JobdClient(base_url=BROKER))
    _, out = _call(server, "jobd_worker_delete", {"host": "ghost"})
    hint = out["error"]["hint"]
    assert "jobd_list" not in hint and "job id" not in hint.lower(), hint
    assert "jobd_workers" in hint, hint
    _, out = _call(server, "jobd_worker_delete", {"host": "laptop"})
    hint = out["error"]["hint"]
    assert "the job" not in hint.lower() and "online" in hint.lower(), hint
    # positive control: job tools keep the job hint
    _, out = _call(server, "jobd_status", {"job_id": 99})
    assert "jobd_list" in out["error"]["hint"], out


def test_l12_every_resource_keyed_hint_covers_every_resource():
    from jobd.mcp.errors import _RULES, RESOURCES
    from jobd.mcp.server import _TOOLS

    for status, _, kind, hint in _RULES:
        if isinstance(hint, dict):
            assert set(hint) == set(RESOURCES), (status, kind, sorted(hint))
    assert {res for *_, res in _TOOLS} <= set(RESOURCES)


def test_l7_cli_cancel_reason_reaches_the_broker(app, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from job_cli import cli as cli_mod

    tc = TestClient(app)
    r = tc.post("/submit", json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"})
    jid = r.json()["id"]

    def _client():
        c = JobdClient(base_url="http://testserver")
        c._client = tc
        return c

    monkeypatch.setattr(cli_mod, "_client", _client)
    r = CliRunner().invoke(cli_mod.app, ["cancel", str(jid), "--reason", "oops"])
    assert r.exit_code == 0, r.output
    ev = [
        json.loads(line)
        for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
        if '"job_cancelled"' in line
    ]
    assert ev and ev[-1]["payload"]["reason"] == "oops", ev


def _sample(prop: dict):
    """A schema-valid sample value for one derived `extra` property."""
    for alt in prop.get("anyOf", [prop]):
        t = alt.get("type")
        if t == "integer":
            return max(1, alt.get("minimum", 1))
        if t == "number":
            return 1.0
        if t == "boolean":
            return True
        if t == "string":
            return "x86_64"
        if t == "object":
            return {"A": "b"}
        if t == "array":
            if alt["items"].get("$ref", "").endswith("SweepAxis"):
                return [{"key": "a", "values": ["1"]}]
            return [1]
    raise AssertionError(f"no sample for {prop}")


def test_every_accepted_extra_key_reaches_the_broker_body():
    """Accepted => forwarded. The schema's `extra` set is derived from the same
    names translate forwards, so a key the MCP accepts can never be dropped
    (the H2/M3 failure was acceptance without forwarding)."""
    from jobd.mcp.schemas import SUBMIT_INPUT
    from jobd.mcp.server import _validate_arguments
    from jobd.mcp.tools import _build_submit_payload

    props = SUBMIT_INPUT["properties"]["extra"]["properties"]
    desc = SUBMIT_INPUT["properties"]["extra"]["description"]
    assert props, "extra lost its derived properties"
    renamed = {"priority": "priority_delta"}
    for key, prop in props.items():
        assert key in desc, f"extra.{key} is accepted but not described"
        args = {"command": "x", "project": "p", "cwd": "/x", "extra": {key: _sample(prop)}}
        _validate_arguments("jobd_submit", args)
        body = xlate_submit_payload(_build_submit_payload(args))
        where = body.get("requires", {}) if key in ("arch", "os", "idempotent") else body
        assert renamed.get(key, key) in where, f"extra.{key} accepted but not forwarded: {body}"


def test_xlate_job_info_passes_signal_through_and_never_invents_it():
    """jobd_status's `signal` is the broker's JobInfo.signal. The translator
    used to setdefault it to None, which reported "no pending signal" for every
    job while the broker omitted the field — so the gap could not show."""
    from jobd.mcp.translate import xlate_job_info

    assert xlate_job_info({"id": 1, "state": "running", "signal": "cancel"})["signal"] == "cancel"
    assert "signal" not in xlate_job_info({"id": 1, "state": "running"})
