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
            "cwd": "/home/mjarnold/jepagame/sweeps",
            "project": "pillar2a1_sweep",
        },
    )
    rows = rooted_client.get("/jobs", params={"project": "pillar2a1_sweep"}).json()
    assert len(rows) == 1
    assert rows[0]["project"] == "jepagame"


def test_filtering_by_the_identity_finds_the_same_job(rooted_client):
    rooted_client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/home/mjarnold/jepagame/sweeps",
            "project": "pillar2a1_sweep",
        },
    )
    rows = rooted_client.get("/jobs", params={"project": "jepagame"}).json()
    assert len(rows) == 1


def test_an_unrelated_project_filter_still_matches_nothing(rooted_client):
    """Positive control: OR-ing two columns is exactly how a filter
    accidentally becomes a no-op that matches everything."""
    rooted_client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/home/mjarnold/jepagame/sweeps",
            "project": "pillar2a1_sweep",
        },
    )
    assert rooted_client.get("/jobs", params={"project": "orchid-sdxl"}).json() == []
