"""Map broker BrokerRefusal exceptions to structured tool-result errors.

Each entry: (status_code, regex on detail) → (kind, hint).
Order matters — first match wins.
"""

from __future__ import annotations

import re

from jobd.client import BrokerRefusal

_RULES: list[tuple[int, re.Pattern, str, str]] = [
    (
        400,
        re.compile(r"cwd .* is under /mnt/c/", re.I),
        "cwd_outside_mount_roots",
        "Pass --host laptop in extra (or move cwd off /mnt/c/).",
    ),
    (
        400,
        re.compile(r"unknown parent job", re.I),
        "unknown_parent",
        "Verify the parent job_id with jobd_status before depending on it.",
    ),
    (
        400,
        re.compile(r"no eligible worker", re.I),
        "no_eligible_worker",
        "Check jobd_workers — host_pin may not match any live worker.",
    ),
    (
        400,
        re.compile(r"required|missing|invalid|must be", re.I),
        "invalid_submit",
        "Broker rejected the request. Read the message for the missing/invalid field.",
    ),
    (
        404,
        re.compile(r".*"),
        "not_found",
        "Job id does not exist. List recent jobs with jobd_list.",
    ),
    (
        409,
        re.compile(r".*"),
        "conflict",
        "State conflict — the job may already be terminal.",
    ),
]


def map_broker_refusal(e: BrokerRefusal) -> dict:
    """Return {kind, message, hint} for a BrokerRefusal."""
    detail = e.detail or ""
    for status, regex, kind, hint in _RULES:
        if e.status_code == status and regex.search(detail):
            return {"kind": kind, "message": detail, "hint": hint}
    return {
        "kind": "unknown",
        "message": detail,
        "hint": f"Unmapped broker {e.status_code}: {detail[:120]}",
    }
