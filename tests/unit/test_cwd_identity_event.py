"""The event, and the registration that makes it observable.

The second test is the one that matters. An event name missing from
KNOWN_EVENTS still reaches events.jsonl, so a test that only greps the log file
passes while the Prometheus counter buckets it as "other" -- and an alert
written against the real name never fires, looking healthy the whole time.
That is why the registration gets its own assertion.
"""

from jobd.models import KNOWN_EVENTS


def test_cwd_identity_applied_is_a_known_event():
    """Not decoration: broker/events.py:74 buckets any unlisted name as
    'other', which silently disarms every alert written against it."""
    assert "cwd_identity_applied" in KNOWN_EVENTS


def test_the_event_is_emitted_with_the_label_and_the_root(rooted_logs):
    import json

    client, logs_dir = rooted_logs
    r = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/home/mjarnold/jepagame/sweeps",
            "project": "pillar2a1_sweep",
        },
    )
    assert r.status_code == 200, r.text
    rows = [
        json.loads(line)
        for line in (logs_dir / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    hits = [e for e in rows if e["event"] == "cwd_identity_applied"]
    assert len(hits) == 1, f"expected exactly one, got {[e['event'] for e in rows]}"
    assert hits[0]["project"] == "jepagame"
    assert hits[0]["payload"]["project_label"] == "pillar2a1_sweep"
    assert hits[0]["payload"]["matched_root"] == "/home/mjarnold/jepagame"


def test_no_event_when_the_typed_name_was_already_registered(rooted_logs):
    """Positive control. Without this, an implementation that fires the event
    on EVERY submit would pass the test above and make the metric useless."""
    import json

    client, logs_dir = rooted_logs
    r = client.post(
        "/submit",
        json={"cmd": ["true"], "cwd": "/home/mjarnold/jepagame", "project": "jepagame"},
    )
    assert r.status_code == 200, r.text
    rows = [
        json.loads(line)
        for line in (logs_dir / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert not [e for e in rows if e["event"] == "cwd_identity_applied"]
