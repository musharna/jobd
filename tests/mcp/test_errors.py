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
        (400, "unknown parent job 99", "unknown_parent"),
        (
            400,
            "no eligible worker for job (host_pin='laptop', no live worker matches)",
            "no_eligible_worker",
        ),
        (400, "command field is required", "invalid_submit"),
        (400, "totally novel rejection text", "unknown"),
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


def test_hint_for_cwd_mentions_host_flag():
    e = BrokerRefusal("x", status_code=400, detail="cwd '/mnt/c/foo' is under /mnt/c/...")
    out = map_broker_refusal(e)
    assert "host" in out["hint"].lower()
