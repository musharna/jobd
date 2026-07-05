"""Map broker BrokerRefusal exceptions to structured tool-result errors.

Each entry: (status_code, regex on detail) → (kind, hint).
Order matters — first match wins.
"""

from __future__ import annotations

import re

from jobd.client import BrokerRefusal

# Regexes are matched against the broker's REAL detail strings (app.py /submit
# validation + matcher.cwd_routability) — audit 2026-07-05 A7 found the old
# "unknown parent job" / "no eligible worker" rules matched nothing the broker
# ever emits, while the actual mount-root hard-fails fell through to "unknown".
_RULES: list[tuple[int, re.Pattern, str, str]] = [
    (
        400,
        re.compile(r"cwd .* is under /mnt/c/", re.I),
        "cwd_outside_mount_roots",
        "Pass --host laptop in extra (or move cwd off /mnt/c/).",
    ),
    (
        400,
        # matcher.cwd_routability: "cwd '…' is under no mount_root of
        # host_pin='…'" / "cwd '…' is under no known worker's mount_roots".
        re.compile(r"cwd .* is under no (mount_root|known worker)", re.I),
        "cwd_outside_mount_roots",
        "No worker advertises a mount root covering this cwd. Pass --host "
        "<a-host-that-has-it> in extra, or stage the data under a shared path.",
    ),
    (
        400,
        # app.py /submit: "depends_on refers to missing job: N".
        re.compile(r"depends_on refers to missing job", re.I),
        "unknown_parent",
        "Verify the parent job_id with jobd_status before depending on it.",
    ),
    (
        400,
        # app.py /submit: "depends_on parent N is already <state> (a
        # failed-side terminal)".
        re.compile(r"depends_on parent \d+ is already", re.I),
        "parent_failed",
        "The parent already reached a failed-side terminal state. Resubmit the "
        "parent, or pass depends_on_any_exit=true to proceed on any exit.",
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
