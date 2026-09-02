"""`--project` must find work under either name.

A filter that matched only the identity would lose every historical query a
human has muscle memory for; one that matched only the label would stop finding
a project's own jobs. Both, or the split is a regression in daily use.
"""


def test_filtering_by_the_label_finds_the_job(rooted_client):
    rooted_client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/home/user/beta/sweeps",
            "project": "pillar2a1_sweep",
        },
    )
    rows = rooted_client.get("/jobs", params={"project": "pillar2a1_sweep"}).json()
    assert len(rows) == 1
    assert rows[0]["project"] == "beta"


def test_filtering_by_the_identity_finds_the_same_job(rooted_client):
    rooted_client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/home/user/beta/sweeps",
            "project": "pillar2a1_sweep",
        },
    )
    rows = rooted_client.get("/jobs", params={"project": "beta"}).json()
    assert len(rows) == 1


def test_an_unrelated_project_filter_still_matches_nothing(rooted_client):
    """Positive control: OR-ing two columns is exactly how a filter
    accidentally becomes a no-op that matches everything."""
    rooted_client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/home/user/beta/sweeps",
            "project": "pillar2a1_sweep",
        },
    )
    assert rooted_client.get("/jobs", params={"project": "gamma"}).json() == []
    assert rooted_client.get("/jobs", params={"project": "gamma"}).json() == []


def test_filtering_by_a_different_spelling_of_the_identity_finds_the_job(rooted_client):
    """audit 2026-09-02 C-1: the filter compared the raw string, so a project
    stored under its registered spelling was unfindable under any other. The
    submit path folds case and -/_ (`BETA` prices as `beta`); the read
    path must fold the same way or one project splits into two views."""
    rooted_client.post(
        "/submit",
        json={"cmd": ["true"], "cwd": "/tmp", "project": "BETA"},
    )
    rooted_client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/home/user/beta/sweeps",
            "project": "pillar2a1_sweep",
        },
    )
    # A third spelling nobody typed: only folding can find these.
    rows = rooted_client.get("/jobs", params={"project": "Beta"}).json()
    assert len(rows) == 2, [(r["project"], r["project_label"]) for r in rows]
    assert {r["project"] for r in rows} == {"beta"}
    # And the label as typed still finds the folded job.
    rows = rooted_client.get("/jobs", params={"project": "BETA"}).json()
    assert len(rows) == 2
    # Positive control: folding must not widen the filter into a no-op.
    assert rooted_client.get("/jobs", params={"project": "gamma"}).json() == []
