"""Translation layer between MCP-facing shapes and the live jobd broker schema.

The MCP surface uses ergonomic field names (`job_id`, `command`, `host`,
`queued_at`) for Claude. The broker uses the SQLAlchemy/Pydantic-native
names (`id`, `cmd`, `worker`, `submitted_at`). This module is the single
point where the two vocabularies meet — every reshape lives here so the
unit tests can stub the live broker shape and the translation logic is
reviewable in one place.

Schema source of truth: `src/jobd/models.py` (`JobInfo`, `WorkerInfo`,
`JobSubmit`).
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

# Broker → MCP field renames for JobInfo.
# (broker_field, mcp_field). cmd/cwd/state/exit_code/etc. are unchanged.
# `depends_on` is intentionally not renamed: the broker already returns it
# as a list[int], the MCP-side input field is also `depends_on`, so output
# matches input — no `_json` suffix to imply serialization that isn't there.
_JOB_RENAMES: list[tuple[str, str]] = [
    ("id", "job_id"),
    ("worker", "host"),
    ("submitted_at", "queued_at"),
]


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def xlate_job_info(broker: dict) -> dict:
    """Translate a broker JobInfo dict to the MCP-facing shape.

    Preserves passthrough fields. Adds `duration_s` (None unless both
    started_at and finished_at are present) and `signal: None` (broker's
    JobInfo has no signal field; jobd_cancel synthesizes it separately).
    """
    out = dict(broker)
    for src, dst in _JOB_RENAMES:
        if src in out:
            out[dst] = out.pop(src)
    started = _parse_iso(out.get("started_at"))
    finished = _parse_iso(out.get("finished_at"))
    if started and finished:
        out["duration_s"] = (finished - started).total_seconds()
    else:
        out["duration_s"] = None
    out.setdefault("signal", None)
    return out


def wrap_jobs(broker_list: list[dict]) -> dict:
    """Bare list[JobInfo] → {jobs: [translated...], counts: {state→count}}.

    `counts` is derived client-side from the returned list — the broker has
    no separate counts endpoint. recent_failed_24h not computed here.
    """
    jobs = [xlate_job_info(j) for j in broker_list]
    counts: dict[str, Any] = dict(Counter(j["state"] for j in jobs))
    return {"jobs": jobs, "counts": counts}


def wrap_workers(broker_list: list[dict]) -> dict:
    """Bare list[WorkerInfo] → {workers: [...]}. Field names already match."""
    return {"workers": list(broker_list)}


def xlate_submit_payload(mcp: dict) -> dict:
    """MCP-flat submit payload → broker JobSubmit body.

    Input shape (after `tools._build_submit_payload`): flat dict that may
    contain command/project/cwd plus any of needs/gpu/host plus arbitrary
    extra keys (idempotent, depends_on, depends_on_any_exit, session_id,
    profile, env, preemptible, priority, max_wall, …).

    Output shape (broker JobSubmit, see `models.py:55`):
      cmd: list[str]                 ← ["bash", "-c", <command>]
      cwd, project                   ← passthrough
      host_pin: str                  ← from MCP `host` (default "any")
      profile, env, preemptible      ← passthrough if present
      session_id                     ← passthrough if present
      depends_on, depends_on_any_exit ← passthrough
      priority_delta                 ← from MCP `priority` if present
      requires: JobRequires          ← {gpu, needs, idempotent} if any set

    Drops unknown keys.
    """
    body: dict[str, Any] = {
        "cmd": ["bash", "-c", mcp["command"]],
        "cwd": mcp["cwd"],
        "project": mcp["project"],
        "host_pin": mcp.get("host", "any"),
    }
    requires: dict[str, Any] = {}
    if mcp.get("gpu") is not None:
        requires["gpu"] = bool(mcp["gpu"])
    if mcp.get("needs"):
        requires["needs"] = list(mcp["needs"])
    if mcp.get("idempotent"):
        requires["idempotent"] = True
    if requires:
        body["requires"] = requires
    for k in (
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
        "count",
        "sweep",
        "dry_run",
    ):
        if k in mcp:
            body[k] = mcp[k]
    if "priority" in mcp:
        body["priority_delta"] = int(mcp["priority"])
    return body
