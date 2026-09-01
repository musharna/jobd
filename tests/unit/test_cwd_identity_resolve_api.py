def test_resolve_reports_the_root_that_supplied_the_identity(rooted_client):
    """/resolve is the dry-run surface: if it cannot say WHY a project was
    chosen, an operator debugging a surprise priority has nothing to read."""
    r = rooted_client.post(
        "/resolve",
        json={
            "cmd": ["true"],
            "cwd": "/home/mjarnold/jepagame/sweeps",
            "project": "pillar2a1_sweep",
        },
    )
    body = r.json()
    assert body["project"] == "jepagame"
    assert body["project_label"] == "pillar2a1_sweep"
    assert body["matched_root"] == "/home/mjarnold/jepagame"


def test_resolve_reports_no_root_for_a_registered_name(rooted_client):
    r = rooted_client.post(
        "/resolve",
        json={"cmd": ["true"], "cwd": "/home/mjarnold/jepagame", "project": "jepagame"},
    )
    assert r.json()["matched_root"] is None
