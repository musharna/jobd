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

from jobd.models import JobRequires, JobSubmit

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


# The MCP submit surface is DENY-listed, not allow-listed. Every JobSubmit field
# is forwarded to the broker under its own name unless it appears here with a
# reason — so `_SUBMIT_PASSTHROUGH` is derived from the broker model rather than
# hand-maintained alongside it.
#
# This inversion is the fix for a class of bug, not one instance. The previous
# code carried a hand-written tuple of forwarded names, and the test that was
# built to guard it asserted against a hand-written `expected_subset` — so both
# halves had to be remembered when a field was added to JobSubmit, and neither
# was. `scheduling_timeout_s` shipped on the broker and the CLI and was silently
# dropped by the MCP surface for every release since; the drift guard could not
# fire, because it only ever checked the fields someone had already remembered.
# Under a deny-list, forgetting is what fails: a new JobSubmit field is either
# forwarded automatically or it must be named here (audit 2026-07-12).
_SUBMIT_SYNTHESIZED: dict[str, str] = {
    "cmd": "built from MCP `command` (wrapped as bash -c)",
    "cwd": "required MCP field, set explicitly",
    "project": "required MCP field, set explicitly",
    "host_pin": "renamed from MCP `host`",
    "priority_delta": "renamed from MCP `priority`",
    "requires": "collapsed from flat MCP gpu/needs/idempotent/arch/os",
    "fast_path": "internal scheduler hint; not caller-settable on any surface",
}

_SUBMIT_PASSTHROUGH: tuple[str, ...] = tuple(
    f for f in JobSubmit.model_fields if f not in _SUBMIT_SYNTHESIZED
)

# JobRequires is nested on the broker but flat on the MCP surface. gpu/needs/
# idempotent need bespoke coercion (below); arch/os are plain strings whose
# broker default is the sentinel "any", so a falsy MCP value means "unset".
_REQUIRES_FLAT_STR: tuple[str, ...] = tuple(
    f for f in JobRequires.model_fields if f not in ("gpu", "needs", "idempotent")
)


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

    Output shape (broker JobSubmit, see `models.py`):
      cmd: list[str]                 ← ["bash", "-c", <command>]
      cwd, project                   ← passthrough
      host_pin: str                  ← from MCP `host` (default "any")
      priority_delta                 ← from MCP `priority` if present
      requires: JobRequires          ← {gpu, needs, idempotent, arch, os} if any set
      everything else in JobSubmit   ← passthrough by name if present
                                       (_SUBMIT_PASSTHROUGH, derived from the model)

    Drops keys that are not JobSubmit fields.
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
    for k in _REQUIRES_FLAT_STR:
        if mcp.get(k):
            requires[k] = str(mcp[k])
    if requires:
        body["requires"] = requires
    for k in _SUBMIT_PASSTHROUGH:
        if k in mcp:
            body[k] = mcp[k]
    if "priority" in mcp:
        body["priority_delta"] = int(mcp["priority"])
    return body
