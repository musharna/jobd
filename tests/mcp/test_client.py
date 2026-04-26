import pytest
from jobd.client import BrokerUnreachable, BrokerServerError, BrokerRefusal


def test_broker_unreachable_is_exception():
    with pytest.raises(BrokerUnreachable):
        raise BrokerUnreachable("connect refused")


def test_broker_server_error_carries_status():
    e = BrokerServerError("oops", status_code=503)
    assert e.status_code == 503


def test_broker_refusal_carries_detail_and_status():
    e = BrokerRefusal(
        "cwd outside mount_roots", status_code=400, detail="cwd '/mnt/c/...' is under /mnt/c/..."
    )
    assert e.status_code == 400
    assert "cwd" in e.detail
