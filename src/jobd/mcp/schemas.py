"""JSON schemas for the 7 MCP tools (per spec §3)."""

from __future__ import annotations

SUBMIT_INPUT = {
    "type": "object",
    "required": ["command", "project", "cwd"],
    "properties": {
        "command": {"type": "string", "description": "Shell command run by the worker shell."},
        "project": {
            "type": "string",
            "description": "Priority lookup key; falls back to _default.",
        },
        "cwd": {
            "type": "string",
            "description": "Absolute path; broker validates against worker mount_roots.",
        },
        "needs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Tool tags (R, python3, heavy-run).",
        },
        "gpu": {"type": "boolean", "default": False, "description": "Pin to GPU-capable worker."},
        "host": {"type": "string", "description": "Host alias pin (laptop, desktop-worker)."},
        "wait": {
            "type": "boolean",
            "default": False,
            "description": "Sync mode: block until terminal or timeout.",
        },
        "wait_timeout_s": {
            "type": "integer",
            "default": 90,
            "description": "Seconds; permissive — server clamps to 270.",
        },
        "extra": {
            "type": "object",
            "description": "Escape hatch: idempotent (bool), depends_on (int[]), depends_on_any_exit (bool), priority (int delta), max_wall (str), profile (str), env (dict).",
            "additionalProperties": True,
        },
    },
}

JOB_ID_ONLY = {
    "type": "object",
    "required": ["job_id"],
    "properties": {"job_id": {"type": "integer"}},
}

STATUS_INPUT = {
    "type": "object",
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
    "required": ["job_id"],
    "properties": {
        "job_id": {"type": "integer"},
        "tail_bytes": {"type": "integer", "default": 8192, "maximum": 1048576},
    },
}

CANCEL_INPUT = {
    "type": "object",
    "required": ["job_id"],
    "properties": {
        "job_id": {"type": "integer"},
        "reason": {"type": "string"},
    },
}

LIST_INPUT = {
    "type": "object",
    "properties": {
        "state": {
            "type": "array",
            "items": {"type": "string"},
            "default": ["queued", "assigned", "running"],
            "description": "States to include. Currently only the first is forwarded to the broker (single state_filter).",
        },
        "project": {"type": "string"},
        "limit": {"type": "integer", "default": 50, "maximum": 200},
    },
}

WORKERS_INPUT = {"type": "object", "properties": {}}
