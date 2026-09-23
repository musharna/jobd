"""JSON schemas for the MCP tools (per spec §3; the authoritative tool list is
server._TOOLS, parity-tested against the broker's live route table)."""

from __future__ import annotations

from typing import Any

from jobd.models import KNOWN_EVENTS, JobRequires, JobState, JobSubmit

# Every schema below is CLOSED (additionalProperties: false) and the server
# validates each call against it before the tool runs (server._dispatch). Until
# 2026-09-22 the schemas were advertisement only: nothing checked them, so a
# key the tool did not read was silently dropped while the call reported
# success — top-level `depends_on`/`env`/`priority` (audit H2), typos inside
# `extra` (M3), a bare string where a list was expected (L9, iterated into
# characters), an unknown state name (M6). One validation layer closes the
# class; per-key copies would only close the instances someone remembered.


def _extra_properties() -> tuple[dict[str, Any], dict[str, Any]]:
    """`extra`'s properties, DERIVED from the broker's own models.

    The accepted set is exactly what `translate.xlate_submit_payload` forwards:
    every JobSubmit passthrough field, `priority` (-> priority_delta), and the
    flat requires fields arch/os/idempotent (-> requires.*). Types and bounds
    come from the pydantic JSON schema, so the MCP rejects what the broker
    would reject, before a round trip. Returns (properties, $defs).
    """
    from jobd.mcp.translate import _REQUIRES_FLAT_STR, _SUBMIT_PASSTHROUGH

    sub = JobSubmit.model_json_schema()
    req = JobRequires.model_json_schema()["properties"]
    props: dict[str, Any] = {
        k: sub["properties"][k] for k in _SUBMIT_PASSTHROUGH if k not in _NOT_IN_EXTRA
    }
    props["priority"] = sub["properties"]["priority_delta"]
    for k in (*_REQUIRES_FLAT_STR, "idempotent"):
        props[k] = req[k]
    return props, sub.get("$defs", {})


# JobSubmit passthrough fields that are deliberately NOT accepted in `extra`.
_NOT_IN_EXTRA: dict[str, str] = {
    "dry_run": "a first-class top-level argument",
    "submitted_via": "always 'mcp' on this surface; set by the server",
}

_EXTRA_PROPERTIES, _SUBMIT_DEFS = _extra_properties()

SUBMIT_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["command", "project", "cwd"],
    "$defs": _SUBMIT_DEFS,
    "properties": {
        "command": {"type": "string", "description": "Shell command run by the worker shell."},
        "project": {
            "type": "string",
            "description": (
                "Scheduling identity. A registered projects.yaml name (matched "
                "case- and -/_-insensitively) prices at its priority; an "
                "unregistered name is priced by the project whose roots: contain "
                "cwd, else by _default. The result's project_label carries the "
                "name as typed when the two differ."
            ),
        },
        "cwd": {
            "type": "string",
            "description": "Absolute path; broker validates against worker mount_roots.",
        },
        "needs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Tool tags (R, python3, cuda).",
        },
        "gpu": {
            "type": "boolean",
            "default": False,
            "description": (
                "true = pin to a GPU-capable worker. false or omitted = no GPU "
                "preference (any worker, GPU or not) — the same as leaving off the "
                "CLI's --gpu. Forbidding GPU workers (CLI --no-gpu) is not offered here."
            ),
        },
        "host": {"type": "string", "description": "Host alias pin (laptop, desktop-vm)."},
        "wait": {
            "type": "boolean",
            "default": False,
            "description": "Sync mode: block until terminal or timeout. For an array submit (count/sweep), waits on every member under one shared deadline and returns an aggregate {array_id, count, job_ids, states, all_completed, members:[{job_id, state, exit_code}]}.",
        },
        "wait_timeout_s": {
            "type": "integer",
            "default": 90,
            "description": "Seconds; permissive — server clamps to 270.",
        },
        "dry_run": {
            "type": "boolean",
            "default": False,
            "description": "Preview mode: run full validation + routing decision (profile, project defaults, cwd, depends_on, preflight, gpu_contention) and return the would-be plan WITHOUT queueing. Response has state='dry-run', would_route_to (list[host]), would_use_worker (host or null), validation (resolved fields + warnings). Per dry-run convention 2026-05-18.",
        },
        "extra": {
            "type": "object",
            "description": "Escape hatch: idempotent (bool), depends_on (int[]), depends_on_any_exit (bool), priority (int delta), max_wall_s (int), idle_timeout_s (int), max_retries (int, default 0: re-run up to N times on a plain non-zero exit), retry_delay_s (int), scheduling_timeout_s (int 1..604800 — give up and terminate the job as 'scheduling_timeout' if it is still QUEUED after N seconds; omit to wait indefinitely for a capable worker), checkpoint_grace_s (int 1..300), vram_gb (float — explicit GPU VRAM the job needs at dispatch; falls back to cuda-Ngb tier-tag max, then to 2 GB floor for --gpu jobs), count (int 1..1000 — submit a job array of N members, with `{i}` in the command replaced by the 0-based index; response is {array_id, count, job_ids, warnings} instead of a single job), sweep (list of {key, values[]} — parameter-sweep axes; broker fans out the cartesian product, substituting `{key}` per member plus `{i}`; mutually exclusive with count; product capped at 1000), profile (str), env (dict), preemptible (bool), session_id (str), arch (str — pin to a worker CPU arch), os (str — pin to a worker OS).",
            "properties": _EXTRA_PROPERTIES,
            "additionalProperties": False,
        },
    },
}

STATUS_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["job_id"],
    "properties": {
        "job_id": {"type": "integer"},
        "wait": {"type": "boolean", "default": False},
        "wait_timeout_s": {
            "type": "integer",
            "default": 90,
            "description": "Server clamps to 270.",
        },
    },
}

LOGS_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["job_id"],
    "properties": {
        "job_id": {
            "type": "integer",
            "description": "Numeric job id whose captured output to read.",
        },
        "tail_bytes": {
            "type": "integer",
            "default": 8192,
            "maximum": 1048576,
            "description": "How many bytes from the END of the log to return (server caps reads at 1 MiB). Raise for context, lower for a quick liveness peek.",
        },
    },
}

CANCEL_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["job_id"],
    "properties": {
        "job_id": {"type": "integer"},
        "reason": {
            "type": "string",
            "description": "Why — recorded on the broker's job_cancelled event (see jobd_events).",
        },
    },
}

PREEMPT_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["job_id"],
    "properties": {
        "job_id": {"type": "integer"},
    },
}

LIST_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "state": {
            "type": "array",
            "items": {"type": "string", "enum": [s.value for s in JobState]},
            "default": ["queued", "assigned", "running"],
            "description": "States to include — any of: queued, assigned, running, completed, failed, cancelled, preempted, orphaned, scheduling_timeout. Omit for the active set (queued/assigned/running); pass [] for all states.",
        },
        "project": {
            "type": "string",
            "description": "Restrict to one project's jobs (the --project value used at submit).",
        },
        "limit": {
            "type": "integer",
            "default": 50,
            "maximum": 200,
            "description": "Max jobs returned (newest first; clamped to [1,200]). `counts` still covers every job matching the filters; a `truncated` field reports how many were cut.",
        },
    },
}

WORKERS_INPUT = {"type": "object", "additionalProperties": False, "properties": {}}

WORKER_DELETE_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "host": {"type": "string", "description": "Worker host identifier."},
    },
    "required": ["host"],
}

EVENTS_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "since": {
            "type": "string",
            "description": "Relative window (2h, 3d, 1w) or an ISO-8601 timestamp. Default: all retained.",
        },
        "event": {
            "type": "string",
            # DERIVED from models.KNOWN_EVENTS (audit 2026-07-15 Q-1): the old
            # hand-typed list was stale within a day of shipping — 7 real event
            # types (job_resurrected among them) simply weren't advertised, so
            # an agent debugging a resurrected job was told the filter value
            # didn't exist. KNOWN_EVENTS itself is completeness-tested against
            # every emit site in the source (AST sweep), closing the loop.
            "description": (
                "Filter to one event type. Known types: "
                + ", ".join(sorted(KNOWN_EVENTS))
                + ". Hook-ingested events may carry custom names beyond these."
            ),
        },
        "job_id": {"type": "integer", "description": "Only events for this job."},
        "project": {"type": "string", "description": "Only events for this project."},
        "source": {
            "type": "string",
            # broker-emitted rows + every EventIngest source the broker accepts.
            # The old ["broker","worker"] enum FORBADE filtering to hook/mcp
            # events even though the broker records and filters them fine.
            "enum": ["broker", "worker", "hook", "mcp"],
            "description": "Which side emitted the event.",
        },
        "limit": {
            "type": "integer",
            "default": 200,
            "description": "Max rows, newest-last. Broker clamps to 10000.",
        },
    },
}
