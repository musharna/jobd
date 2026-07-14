"""Every broker status an agent can hit must map to an actionable kind — not "unknown".

`JobdClient._request` raises `BrokerRefusal` for **any** 4xx. The MCP layer then maps it
to `{kind, message, hint}`. Until 2026-07-13 the rule table covered 400/404/409 only, so
401 (bad token), 403 (tailnet ACL) and 422 (malformed body) fell through to
`kind="unknown"` with the hint "Unmapped broker 401: ..." — an LLM given that has nothing
to act on, and worse, no way to know the failure is *permanent* rather than worth a retry.

The guard below is derived rather than hand-listed, which is the same shape as the
`_SUBMIT_SYNTHESIZED` deny-list that fixed the /submit field drift: adding a new broker
status without mapping it FAILS here instead of silently degrading an agent. Forgetting
is what fails.
"""

from __future__ import annotations

import pytest

from jobd.client import BrokerRefusal
from jobd.mcp.errors import _DELIBERATELY_UNMAPPED, map_broker_refusal

# Every 4xx the broker actually returns. Sourced from the routes:
#   400 validation (submit), 401 auth.require_token, 403 tailnet ACL,
#   404 unknown job/worker, 409 state conflict, 422 FastAPI body validation.
BROKER_STATUSES = [400, 401, 403, 404, 409, 422]


@pytest.mark.parametrize("status", BROKER_STATUSES)
def test_every_broker_status_maps_to_an_actionable_kind(status: int):
    mapped = map_broker_refusal(
        BrokerRefusal(f"broker {status}", status_code=status, detail="something went wrong")
    )
    assert mapped["kind"] != "unknown", (
        f"broker {status} falls through to kind='unknown'. An agent receiving that has "
        "nothing to act on and cannot tell a permanent failure from a retryable one. Add "
        "a rule to jobd/mcp/errors.py::_RULES, or record it in _DELIBERATELY_UNMAPPED "
        "with a reason."
    )
    assert mapped["hint"], f"broker {status} maps to kind={mapped['kind']!r} with no hint"


@pytest.mark.parametrize("status", BROKER_STATUSES)
def test_no_status_is_both_mapped_and_excused(status: int):
    """A status cannot be in the deny-list and also produce a real kind — that means the
    deny-list entry is stale and its stated reason is now a lie."""
    if status in _DELIBERATELY_UNMAPPED:
        mapped = map_broker_refusal(BrokerRefusal("x", status_code=status, detail="x"))
        assert mapped["kind"] == "unknown", (
            f"{status} is listed in _DELIBERATELY_UNMAPPED ('{_DELIBERATELY_UNMAPPED[status]}') "
            "but it DOES map to a real kind. Remove the stale deny-list entry."
        )


def test_auth_and_acl_hints_say_the_failure_is_permanent():
    """401/403/422 are configuration errors. An agent must not be encouraged to retry.

    This is the whole point of mapping them: a retry loop against a bad token is the
    worst outcome, and 'Unmapped broker 401' actively invites one.
    """
    for status in (401, 403, 422):
        hint = map_broker_refusal(BrokerRefusal("x", status_code=status, detail="x"))["hint"]
        assert "will not fix it" in hint, (
            f"the hint for {status} does not tell the agent that retrying is futile: {hint!r}"
        )


def test_detail_rules_still_win_over_the_status_fallbacks():
    """The specific 400 rules must not be shadowed by the broad ones added alongside."""
    mapped = map_broker_refusal(
        BrokerRefusal(
            "broker 400",
            status_code=400,
            detail="cwd '/mnt/c/foo' is under /mnt/c/ (Windows mount, laptop-only)",
        )
    )
    assert mapped["kind"] == "cwd_outside_mount_roots", mapped
