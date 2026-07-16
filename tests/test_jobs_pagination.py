"""GET /jobs pagination + `job list` default cap (audit 2026-07-12).

`GET /jobs` used to return EVERY row ever — on a broker with retention off that
is the entire history. The MCP surface was already working around it by fetching
everything and truncating client-side; the human `job list` had no cap at all.

`limit` is deliberately OPT-IN (None = all): `graph` and `--array` build over
the complete set, and a silent default cap at the API layer would quietly
corrupt them. The bound is applied by the *callers* that render to a human, and
the full filtered count always comes back in `X-Total-Count`.
"""

from __future__ import annotations


def _submit(client, n):
    ids = []
    for i in range(n):
        r = client.post(
            "/submit",
            json={"cmd": ["echo", str(i)], "cwd": "/tmp", "project": "project-a"},
        )
        assert r.status_code == 200, r.text
        ids.append(r.json()["id"])
    return ids


def test_no_limit_returns_everything_and_reports_total(client):
    _submit(client, 5)
    r = client.get("/jobs")
    assert r.status_code == 200
    assert len(r.json()) == 5
    assert r.headers["X-Total-Count"] == "5"


def test_limit_caps_the_page_but_total_is_the_full_count(client):
    _submit(client, 5)
    r = client.get("/jobs", params={"limit": 2})
    assert len(r.json()) == 2
    # The header reports the FULL filtered set, not the page — that's what lets
    # a caller honestly say "showing 2 of 5" instead of silently truncating.
    assert r.headers["X-Total-Count"] == "5"


def test_newest_first_and_offset_pages_through(client):
    ids = _submit(client, 5)
    page1 = [j["id"] for j in client.get("/jobs", params={"limit": 2}).json()]
    page2 = [j["id"] for j in client.get("/jobs", params={"limit": 2, "offset": 2}).json()]
    assert page1 == list(reversed(ids))[:2]  # newest first
    assert page2 == list(reversed(ids))[2:4]
    assert not set(page1) & set(page2)  # no overlap


def test_total_respects_the_filters(client):
    _submit(client, 3)
    r = client.get("/jobs", params={"project": "project-a", "limit": 1})
    assert r.headers["X-Total-Count"] == "3"
    r = client.get("/jobs", params={"project": "nope", "limit": 1})
    assert r.json() == []
    assert r.headers["X-Total-Count"] == "0"


def test_limit_is_bounded_server_side(client):
    r = client.get("/jobs", params={"limit": 10_000})
    assert r.status_code == 422  # above LIST_LIMIT_MAX
    assert client.get("/jobs", params={"limit": 0}).status_code == 422
