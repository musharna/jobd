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


def test_xlate_submit_round_trips_every_jobsubmit_field():
    """#51 round-trip: every first-class JobSubmit field that has an MCP-side
    representation must survive xlate_submit_payload. Guards against the
    silent-drop class that kept session_id (and now submitted_via) invisible.

    Fields excluded from the MCP shape by design:
      cmd       — synthesized from `command` (bash -c)
      host_pin  — renamed from MCP `host`
      priority_delta — renamed from MCP `priority`
      requires  — collapsed from gpu/needs/idempotent
      fast_path — internal scheduler hint, not MCP-exposed
    """
    from jobd.models import JobSubmit

    mcp_input = {
        "command": "echo hi",
        "project": "p",
        "cwd": "/x",
        "host": "desktop",
        "gpu": True,
        "needs": ["cuda-8gb"],
        "priority": 5,
        "profile": "default",
        "env": {"K": "v"},
        "preemptible": True,
        "session_id": "sess-1",
        "submitted_via": "mcp",
        "depends_on": [1, 2],
        "depends_on_any_exit": True,
        "max_wall_s": 60,
        "idle_timeout_s": 30,
        "checkpoint_grace_s": 10,
        "vram_gb": 4.0,
    }
    body = xlate_submit_payload(mcp_input)
    JobSubmit.model_validate(body)  # broker must accept it as-is
    expected_subset = {
        "cmd",
        "cwd",
        "project",
        "host_pin",
        "priority_delta",
        "profile",
        "env",
        "preemptible",
        "session_id",
        "submitted_via",
        "depends_on",
        "depends_on_any_exit",
        "max_wall_s",
        "idle_timeout_s",
        "checkpoint_grace_s",
        "vram_gb",
        "requires",
    }
    missing = expected_subset - set(body)
    assert not missing, f"translate dropped: {missing}"
