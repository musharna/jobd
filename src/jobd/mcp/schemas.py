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
            "description": "Tool tags (R, python3, cuda).",
        },
        "gpu": {"type": "boolean", "default": False, "description": "Pin to GPU-capable worker."},
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
            "description": "Escape hatch: idempotent (bool), depends_on (int[]), depends_on_any_exit (bool), priority (int delta), max_wall_s (int), idle_timeout_s (int), checkpoint_grace_s (int 1..300), vram_gb (float — explicit GPU VRAM the job needs at dispatch; falls back to cuda-Ngb tier-tag max, then to 2 GB floor for --gpu jobs), count (int 1..1000 — submit a job array of N members, with `{i}` in the command replaced by the 0-based index; response is {array_id, count, job_ids, warnings} instead of a single job), sweep (list of {key, values[]} — parameter-sweep axes; broker fans out the cartesian product, substituting `{key}` per member plus `{i}`; mutually exclusive with count; product capped at 1000), profile (str), env (dict), preemptible (bool).",
            "additionalProperties": True,
        },
    },
}

JOB_ID_ONLY = {
    "type": "object",
    "required": ["job_id"],
    "properties": {
        "job_id": {
            "type": "integer",
            "description": "Numeric job id as returned by jobd_submit or shown in jobd_list.",
        }
    },
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
    "required": ["job_id"],
    "properties": {
        "job_id": {"type": "integer"},
        "reason": {"type": "string"},
    },
}

PREEMPT_INPUT = {
    "type": "object",
    "required": ["job_id"],
    "properties": {
        "job_id": {"type": "integer"},
    },
}

LIST_INPUT = {
    "type": "object",
    "properties": {
        "state": {
            "type": "array",
            "items": {"type": "string"},
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

WORKERS_INPUT = {"type": "object", "properties": {}}

WORKER_DELETE_INPUT = {
    "type": "object",
    "properties": {
        "host": {"type": "string", "description": "Worker host identifier."},
    },
    "required": ["host"],
}
