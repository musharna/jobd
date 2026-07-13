"""Unit tests for the broker↔MCP translation layer.

These tests anchor against the live broker schema (`src/jobd/models.py`)
not the fixtures we hallucinated in the original spec. If the broker
schema drifts, these break first.
"""

from __future__ import annotations

from jobd.mcp.translate import (
    wrap_jobs,
    wrap_workers,
    xlate_job_info,
    xlate_submit_payload,
)


def test_xlate_job_info_renames_id_worker_submitted_at():
    broker = {
        "id": 42,
        "state": "running",
        "project": "p",
        "worker": "desktop",
        "submitted_at": "2026-04-26T00:00:00+00:00",
        "started_at": "2026-04-26T00:00:01+00:00",
        "finished_at": None,
        "exit_code": None,
        "depends_on": [3, 5],
    }
    out = xlate_job_info(broker)
    assert out["job_id"] == 42
    assert out["host"] == "desktop"
    assert out["queued_at"] == "2026-04-26T00:00:00+00:00"
    assert out["depends_on"] == [3, 5]
    assert "id" not in out
    assert "worker" not in out
    assert "submitted_at" not in out
    assert out["signal"] is None  # broker has no signal field


def test_xlate_job_info_computes_duration_when_both_timestamps_present():
    broker = {
        "id": 1,
        "state": "completed",
        "started_at": "2026-04-26T00:00:00+00:00",
        "finished_at": "2026-04-26T00:00:30+00:00",
    }
    out = xlate_job_info(broker)
    assert out["duration_s"] == 30.0


def test_xlate_job_info_duration_none_when_unfinished():
    out = xlate_job_info({"id": 1, "state": "running", "started_at": "2026-04-26T00:00:00Z"})
    assert out["duration_s"] is None


def test_wrap_jobs_derives_counts_from_states():
    bare_list = [
        {"id": 1, "state": "queued"},
        {"id": 2, "state": "queued"},
        {"id": 3, "state": "running"},
        {"id": 4, "state": "completed"},
    ]
    out = wrap_jobs(bare_list)
    assert out["counts"] == {"queued": 2, "running": 1, "completed": 1}
    assert len(out["jobs"]) == 4
    assert out["jobs"][0]["job_id"] == 1


def test_wrap_workers_passthrough():
    bare = [{"host": "desktop", "last_heartbeat": "2026-04-26T00:00:00Z"}]
    out = wrap_workers(bare)
    assert out["workers"] == bare


def test_xlate_submit_wraps_command_in_bash_c():
    body = xlate_submit_payload({"command": "echo hi", "project": "p", "cwd": "/x"})
    assert body["cmd"] == ["bash", "-c", "echo hi"]
    assert body["project"] == "p"
    assert body["cwd"] == "/x"
    assert body["host_pin"] == "any"


def test_xlate_submit_renames_host_to_host_pin():
    body = xlate_submit_payload({"command": "x", "project": "p", "cwd": "/x", "host": "laptop"})
    assert body["host_pin"] == "laptop"
    assert "host" not in body


def test_xlate_submit_collapses_gpu_needs_idempotent_into_requires():
    body = xlate_submit_payload(
        {
            "command": "x",
            "project": "p",
            "cwd": "/x",
            "gpu": True,
            "needs": ["python3"],
            "idempotent": True,
        }
    )
    assert body["requires"] == {"gpu": True, "needs": ["python3"], "idempotent": True}
    assert "gpu" not in body
    assert "needs" not in body
    assert "idempotent" not in body


def test_xlate_submit_omits_requires_when_unset():
    body = xlate_submit_payload({"command": "x", "project": "p", "cwd": "/x"})
    assert "requires" not in body


def test_xlate_submit_passes_through_depends_on_and_session_id():
    body = xlate_submit_payload(
        {
            "command": "x",
            "project": "p",
            "cwd": "/x",
            "depends_on": [1, 2],
            "depends_on_any_exit": True,
            "session_id": "abc",
        }
    )
    assert body["depends_on"] == [1, 2]
    assert body["depends_on_any_exit"] is True
    assert body["session_id"] == "abc"


def test_xlate_submit_drops_unknown_keys():
    """Keys the broker doesn't recognize are silently dropped."""
    body = xlate_submit_payload({"command": "x", "project": "p", "cwd": "/x", "bogus": "v"})
    assert "bogus" not in body


def test_xlate_submit_passes_through_timeout_fields():
    """#39: max_wall_s / idle_timeout_s / checkpoint_grace_s are first-class
    JobSubmit fields and must pass through the MCP translation layer."""
    body = xlate_submit_payload(
        {
            "command": "x",
            "project": "p",
            "cwd": "/x",
            "max_wall_s": 3600,
            "idle_timeout_s": 600,
            "checkpoint_grace_s": 120,
        }
    )
    assert body["max_wall_s"] == 3600
    assert body["idle_timeout_s"] == 600
    assert body["checkpoint_grace_s"] == 120


def test_xlate_submit_passes_through_vram_gb():
    """#41d: vram_gb is a first-class JobSubmit field and must pass through
    the MCP translation layer (same silent-drop class as #39 timeouts)."""
    body = xlate_submit_payload({"command": "x", "project": "p", "cwd": "/x", "vram_gb": 12.0})
    assert body["vram_gb"] == 12.0


def test_xlate_submit_passes_through_count():
    """Job arrays: `count` is a first-class JobSubmit field and must survive
    the MCP translate seam so an agent can submit an array via jobd_submit."""
    body = xlate_submit_payload({"command": "echo {i}", "project": "p", "cwd": "/x", "count": 5})
    assert body["count"] == 5


def test_xlate_submit_passes_through_sweep():
    """Parameter sweeps: `sweep` axes must survive the MCP translate seam so an
    agent can submit a sweep array via jobd_submit."""
    axes = [{"key": "lr", "values": ["0.1", "0.01"]}, {"key": "seed", "values": ["1", "2"]}]
    body = xlate_submit_payload(
        {"command": "echo {lr} {seed}", "project": "p", "cwd": "/x", "sweep": axes}
    )
    assert body["sweep"] == axes


def test_xlate_submit_priority_to_priority_delta():
    body = xlate_submit_payload({"command": "x", "project": "p", "cwd": "/x", "priority": 10})
    assert body["priority_delta"] == 10
    assert "priority" not in body


def test_xlate_submit_passes_through_submitted_via():
    """#51: submitted_via must survive the translate seam so the broker can
    distinguish CLI vs MCP traffic. Without it, the question 'are sessions
    actually using the MCP?' is unanswerable from the broker DB."""
    body = xlate_submit_payload(
        {"command": "x", "project": "p", "cwd": "/x", "submitted_via": "mcp"}
    )
    assert body["submitted_via"] == "mcp"


# A valid MCP-flat value for every JobSubmit field the MCP surface forwards.
# Keyed by the BROKER field name (which is also the MCP name for passthrough
# fields). Kept beside the tests, not inline, so the "did you cover the new
# field?" assertion below has something concrete to check against.
_MCP_SAMPLE: dict[str, object] = {
    "profile": "default",
    "env": {"K": "v"},
    "preemptible": True,
    "session_id": "sess-1",
    "submitted_via": "mcp",
    "depends_on": [1, 2],
    "depends_on_any_exit": True,
    "vram_gb": 4.0,
    "max_wall_s": 60,
    "idle_timeout_s": 30,
    "scheduling_timeout_s": 900,
    "checkpoint_grace_s": 10,
    "count": 1,
    "sweep": [],
    "dry_run": False,
}


def test_submit_field_taxonomy_is_exhaustive():
    """Every JobSubmit field is EITHER deny-listed with a reason OR forwarded.

    This is the guard the old hand-written `expected_subset` could never be.
    Adding a field to JobSubmit without deciding what the MCP surface does with
    it now fails here — where the previous test passed happily, because it only
    checked the fields someone had already remembered to list. That is exactly
    how `scheduling_timeout_s` stayed un-forwarded for releases while a test
    named "round trips every JobSubmit field" stayed green (audit 2026-07-12).
    """
    from jobd.mcp.translate import _SUBMIT_PASSTHROUGH, _SUBMIT_SYNTHESIZED
    from jobd.models import JobSubmit

    classified = set(_SUBMIT_SYNTHESIZED) | set(_SUBMIT_PASSTHROUGH)
    unclassified = set(JobSubmit.model_fields) - classified
    assert not unclassified, (
        f"new JobSubmit field(s) {sorted(unclassified)} are neither forwarded nor "
        f"deny-listed in translate._SUBMIT_SYNTHESIZED — decide which, with a reason"
    )
    # The deny-list may not name fields that no longer exist: a rename would
    # otherwise silently resurrect the very drop it was written to prevent.
    stale = set(_SUBMIT_SYNTHESIZED) - set(JobSubmit.model_fields)
    assert not stale, f"deny-list names non-existent JobSubmit field(s): {sorted(stale)}"


def test_xlate_submit_round_trips_every_forwarded_jobsubmit_field():
    """#51 round-trip, now derived from the model: every forwarded JobSubmit
    field must survive xlate_submit_payload with its value intact, and the
    result must validate as a real JobSubmit."""
    from jobd.mcp.translate import _SUBMIT_PASSTHROUGH
    from jobd.models import JobSubmit

    uncovered = set(_SUBMIT_PASSTHROUGH) - set(_MCP_SAMPLE)
    assert not uncovered, (
        f"forwarded field(s) {sorted(uncovered)} have no sample value — add one to "
        f"_MCP_SAMPLE so this test actually exercises the round-trip"
    )

    mcp_input: dict[str, object] = {
        "command": "echo hi",
        "project": "p",
        "cwd": "/x",
        # inputs for the deny-listed (synthesized/renamed/collapsed) fields
        "host": "desktop",
        "gpu": True,
        "needs": ["cuda-8gb"],
        "idempotent": True,
        "arch": "x86_64",
        "os": "linux",
        "priority": 5,
        **_MCP_SAMPLE,
    }
    body = xlate_submit_payload(mcp_input)
    JobSubmit.model_validate(body)  # broker must accept it as-is

    missing = set(_SUBMIT_PASSTHROUGH) - set(body)
    assert not missing, f"translate dropped: {sorted(missing)}"
    for field in _SUBMIT_PASSTHROUGH:
        assert body[field] == _MCP_SAMPLE[field], f"{field} mangled in transit"

    assert body["cmd"] == ["bash", "-c", "echo hi"]
    assert body["host_pin"] == "desktop"
    assert body["priority_delta"] == 5
    assert body["requires"] == {
        "gpu": True,
        "needs": ["cuda-8gb"],
        "idempotent": True,
        "arch": "x86_64",
        "os": "linux",
    }


def test_xlate_submit_forwards_scheduling_timeout_s():
    """The specific field the old allow-list dropped. The broker has had
    scheduling_timeout_s since the 2026-05-18 runtime-zombies audit and the CLI
    exposes it; the MCP surface silently discarded it, so an agent could not put
    a stuck-queue guard on a job it submitted."""
    body = xlate_submit_payload(
        {"command": "x", "project": "p", "cwd": "/x", "scheduling_timeout_s": 300}
    )
    assert body["scheduling_timeout_s"] == 300


def test_xlate_submit_collapses_arch_and_os_into_requires():
    """requires.arch / requires.os are real matcher inputs (worker capability
    tags) that the MCP surface previously stranded."""
    body = xlate_submit_payload(
        {"command": "x", "project": "p", "cwd": "/x", "arch": "aarch64", "os": "darwin"}
    )
    assert body["requires"] == {"arch": "aarch64", "os": "darwin"}


def test_every_forwarded_field_is_documented_to_the_model():
    """A field the MCP schema never mentions is, to an LLM, a field that does
    not exist — forwarding it in code is necessary but not sufficient. Assert
    every forwarded field is named somewhere in SUBMIT_INPUT (as a first-class
    property or in the `extra` escape-hatch description)."""
    import json

    from jobd.mcp.schemas import SUBMIT_INPUT
    from jobd.mcp.translate import _SUBMIT_PASSTHROUGH

    # Set by tools._build_submit_payload, not by the caller — deliberately not
    # advertised, so the model can't spoof the submission-origin marker.
    auto_set = {"submitted_via"}

    schema_text = json.dumps(SUBMIT_INPUT)
    undocumented = [f for f in _SUBMIT_PASSTHROUGH if f not in auto_set and f not in schema_text]
    assert not undocumented, (
        f"forwarded but undocumented in SUBMIT_INPUT: {sorted(undocumented)} — the model "
        f"cannot use a field it is never told about"
    )
