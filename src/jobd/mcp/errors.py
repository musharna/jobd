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
    # Catch-all for a 400 none of the specific rules above recognise. Without this a
    # broker validation message we have not seen before degrades an agent to
    # kind="unknown" — which is exactly the hole the 401/403/422 gap left.
    (
        400,
        re.compile(r".*"),
        "bad_request",
        "The broker rejected the request as invalid. The message says why; fix the "
        "arguments rather than retrying them unchanged.",
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
    # --- Transport/auth/validation statuses. -------------------------------------
    # These are status-level, not detail-level: the broker's message is not worth
    # regexing, and an agent that gets kind="unknown" here has nothing to act on.
    # 401/403/422 previously fell straight through to the "unknown" fallback (audit
    # 2026-07-12), so an LLM holding a bad token was told only "Unmapped broker 401".
    (
        401,
        re.compile(r".*"),
        "auth_failed",
        "The broker rejected the bearer token. JOBD_API_TOKEN is unset, wrong, or "
        "stale — check the env of the MCP server process (not the shell you launched "
        "it from). This is a configuration problem; retrying will not fix it.",
    ),
    (
        403,
        re.compile(r".*"),
        "forbidden",
        "The broker's tailnet ACL rejected this request's source IP. The MCP server "
        "must reach the broker over Tailscale (100.64.0.0/10) or loopback — not over "
        "the LAN or a docker bridge. Retrying will not fix it.",
    ),
    (
        422,
        re.compile(r".*"),
        "invalid_arguments",
        "The broker rejected the request body as malformed — a tool argument has the "
        "wrong type or an unknown field. Re-read the tool's input schema; the message "
        "names the offending field. Retrying the same arguments will not fix it.",
    ),
]

# Statuses the client can raise as a BrokerRefusal (any 4xx; see JobdClient._request)
# that we accept as legitimately unmapped. Everything else MUST resolve to a real kind.
#
# This is the deny-list pattern that fixed the /submit field drift: coverage is
# DERIVED and the exceptions are named, so adding a new broker status without mapping
# it FAILS a test rather than silently degrading an agent to kind="unknown". Forgetting
# is what fails. See tests/mcp/test_error_model.py.
_DELIBERATELY_UNMAPPED: dict[int, str] = {
    405: "method not allowed — a client bug, not something an agent can act on",
    413: "payload too large — only reachable via /log, which no MCP tool calls",
    429: "not emitted: the broker has no rate limiter today",
}


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
