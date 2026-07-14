"""map_broker_refusal drives its regexes against the broker's REAL detail
strings (app.py /submit validation, matcher.cwd_routability) — the audit
2026-07-05 A7 finding was rules written against imagined text: "unknown parent
job" and "no eligible worker" matched nothing the broker ever emits, while the
actual mount-root hard-fails fell through to kind="unknown". Every 400 detail
below is copied from the emitting source, so a broker wording change that
breaks the mapping breaks this file."""

import pytest

from jobd.client import BrokerRefusal
from jobd.mcp.errors import map_broker_refusal


@pytest.mark.parametrize(
    "status,detail,expected_kind",
    [
        (
            400,
            "cwd '/mnt/c/Users/x' is under /mnt/c/ (Windows mount, laptop-only) but host_pin='any'. Pass --host laptop, or stage data ...",
            "cwd_outside_mount_roots",
        ),
        (
            400,
            # matcher.cwd_routability, pinned-host branch
            "cwd '/data/run7' is under no mount_root of host_pin='desktop' (roots: ['/home', '/tmp']). Pass --host <a-host-that-has-it>, or stage the data under a path that host advertises.",
            "cwd_outside_mount_roots",
        ),
        (
            400,
            # matcher.cwd_routability, host_pin=any branch
            "cwd '/scratch/x' is under no known worker's mount_roots; no host can run it. Pass --host <the-host-that-has-it>, or stage under a shared path (e.g. /tmp).",
            "cwd_outside_mount_roots",
        ),
        (
            400,
            # app.py /submit dep-existence check
            "depends_on refers to missing job: 99",
            "unknown_parent",
        ),
        (
            400,
            # app.py /submit terminal-parent reject (H2 fix)
            "depends_on parent 41 is already failed (a failed-side terminal); a default-policy dependent would never dispatch. Resubmit the parent, or pass depends_on_any_exit to proceed on any terminal state.",
            "parent_failed",
        ),
        (400, "command field is required", "invalid_submit"),
        # A 400 whose message we have not seen before is still a BAD REQUEST — the one
        # thing we know for certain is that re-sending the same arguments will not help.
        # It used to map to "unknown", which told an agent nothing and left it free to
        # retry forever. (Changed 2026-07-13 alongside the 401/403/422 mappings.)
        (400, "totally novel rejection text", "bad_request"),
        (404, "no such job", "not_found"),
        (409, "cannot cancel terminal job", "conflict"),
    ],
)
def test_map_broker_refusal_kinds(status, detail, expected_kind):
    e = BrokerRefusal("x", status_code=status, detail=detail)
    out = map_broker_refusal(e)
    assert out["kind"] == expected_kind
    assert out["message"] == detail
    assert "hint" in out


def test_missing_parent_beats_generic_invalid_submit():
    """'depends_on refers to missing job' contains 'missing', so rule order
    matters: the specific unknown_parent rule must win over invalid_submit."""
    e = BrokerRefusal("x", status_code=400, detail="depends_on refers to missing job: 7")
    assert map_broker_refusal(e)["kind"] == "unknown_parent"


def test_hint_for_cwd_mentions_host_flag():
    e = BrokerRefusal("x", status_code=400, detail="cwd '/mnt/c/foo' is under /mnt/c/...")
    out = map_broker_refusal(e)
    assert "host" in out["hint"].lower()
