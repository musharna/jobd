"""MCP error mapping driven by REAL broker refusals (audit 2026-07-15 T-LOW-5).

tests/mcp/test_errors.py checks the regex rules against detail strings that
were hand-copied from the emitting source — faithful snapshots, but snapshots:
if the broker rewords a 400, production degrades to kind="bad_request" while
those tests stay green (the historic A7 failure, one drift later).

This file closes the loop: drive the REAL app to emit each mapped refusal,
build the same BrokerRefusal the client would raise, and assert the mapper
still resolves the specific kind. A reworded broker message now fails HERE.
"""

from __future__ import annotations

from jobd.client import BrokerRefusal
from jobd.mcp.errors import map_broker_refusal


def _refusal(resp) -> BrokerRefusal:
    """Exactly what JobdClient._request raises for this response."""
    assert 400 <= resp.status_code < 500, f"expected a refusal, got {resp.status_code}: {resp.text}"
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    return BrokerRefusal(
        f"broker {resp.status_code}", status_code=resp.status_code, detail=detail or ""
    )


_SUBMIT = {"cmd": ["true"], "cwd": "/tmp", "project": "project-a"}


def test_unknown_parent_maps_from_the_real_detail(client):
    resp = client.post("/submit", json={**_SUBMIT, "depends_on": [999999]})
    kind = map_broker_refusal(_refusal(resp))["kind"]
    assert kind == "unknown_parent", (
        f"the broker's real missing-parent detail no longer matches the mapper "
        f"(got kind={kind!r}, detail={resp.json()['detail']!r}) — agents are back "
        "to a generic bad_request for a specific, actionable failure"
    )


def test_parent_failed_maps_from_the_real_detail(client):
    parent = client.post("/submit", json=_SUBMIT).json()["id"]
    assert client.post(f"/jobs/{parent}/cancel").status_code == 200  # queued → cancelled

    resp = client.post("/submit", json={**_SUBMIT, "depends_on": [parent]})
    kind = map_broker_refusal(_refusal(resp))["kind"]
    assert kind == "parent_failed", (
        f"the broker's real terminal-parent detail no longer matches the mapper "
        f"(got kind={kind!r}, detail={resp.json()['detail']!r})"
    )


def test_not_found_maps_from_the_real_detail(client):
    resp = client.get("/jobs/999999")
    assert map_broker_refusal(_refusal(resp))["kind"] == "not_found"


def test_invalid_body_maps_to_invalid_arguments(client):
    resp = client.post("/submit", json={"cwd": "/tmp"})  # missing cmd/project → 422
    assert map_broker_refusal(_refusal(resp))["kind"] == "invalid_arguments"
