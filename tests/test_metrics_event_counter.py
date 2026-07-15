"""M-3 (audit 2026-07-10): /metrics exposes a cumulative event Counter.

The gauges (jobd_jobs/jobd_workers) are point-in-time; cumulative failure/
throughput signals were only in events.jsonl, so Prometheus couldn't alert on
rates. Every event funnels through broker.events._emit_event, which now
increments jobd_events_total{event,source}. This test pins that a submit shows
up on /metrics and that the counter is cumulative.
"""

import re

import pytest
from fastapi.testclient import TestClient

from jobd.app import build_app


@pytest.fixture
def client(tmp_path, sample_projects_yaml, sample_profiles_yaml, sample_classifier_yaml):
    app = build_app(
        db_url=f"sqlite:///{tmp_path}/jobd.db",
        projects_path=sample_projects_yaml,
        profiles_path=sample_profiles_yaml,
        classifier_path=sample_classifier_yaml,
        logs_path=tmp_path / "logs",
    )
    return TestClient(app)


def _counter(metrics_text: str, event: str, source: str = "broker") -> float:
    # jobd_events_total{event="job_submitted",source="broker"} 3.0
    pat = re.compile(
        r'^jobd_events_total\{[^}]*event="'
        + re.escape(event)
        + r'"[^}]*source="'
        + re.escape(source)
        + r'"[^}]*\}\s+([0-9.e+]+)',
        re.MULTILINE,
    )
    m = pat.search(metrics_text)
    return float(m.group(1)) if m else 0.0


def test_submit_increments_event_counter(client):
    before = _counter(client.get("/metrics").text, "job_submitted")

    client.post("/submit", json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"})

    after_text = client.get("/metrics").text
    after = _counter(after_text, "job_submitted")
    assert after == before + 1, after_text

    # The counter family is present and labelled (rate-alertable).
    assert "jobd_events_total" in after_text


# --- label cardinality (audit 2026-07-15 Sec-A) ------------------------------
#
# `event` arrives free-form from POST /events (hooks name their own events),
# and every distinct label value is a PERMANENT time series in the in-process
# registry. Unbounded names = a memory-exhaustion DoS available to any token
# holder or one buggy hook loop. Unknown names share the "other" bucket;
# events.jsonl keeps the real name.


def test_unknown_event_names_share_one_metric_bucket(client, tmp_path):
    # Delta, not absolute: the Counter registry is process-global by design and
    # other tests in the same run may already have bucketed hook events.
    before = _counter(client.get("/metrics").text, "other", source="hook")
    for i in range(5):
        r = client.post(
            "/events",
            json={"source": "hook", "event": f"my_custom_hook_event_{i}", "payload": {}},
        )
        assert r.status_code == 204, r.text

    text = client.get("/metrics").text
    assert _counter(text, "other", source="hook") == before + 5.0, text
    assert "my_custom_hook_event_" not in text, (
        "a free-form ingest name minted its own Prometheus time series — "
        "unbounded label cardinality is a broker-OOM DoS"
    )


def test_known_ingested_events_keep_their_own_label(client):
    before = _counter(client.get("/metrics").text, "watchdog_fired", source="worker")
    r = client.post(
        "/events",
        json={"source": "worker", "event": "watchdog_fired", "payload": {}},
    )
    assert r.status_code == 204
    after = _counter(client.get("/metrics").text, "watchdog_fired", source="worker")
    assert after == before + 1.0


def test_the_jsonl_row_keeps_the_real_name_even_when_the_label_is_bucketed(client, tmp_path):
    import json as _json

    r = client.post(
        "/events",
        json={"source": "hook", "event": "totally_custom", "payload": {}},
    )
    assert r.status_code == 204
    rows = [
        _json.loads(ln)
        for ln in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
        if ln.strip()
    ]
    assert any(row["event"] == "totally_custom" for row in rows), (
        "bucketing must be metric-label-only; the ledger keeps full fidelity"
    )


def test_an_unbounded_event_name_is_rejected_at_the_schema(client):
    r = client.post(
        "/events",
        json={"source": "hook", "event": "x" * 65, "payload": {}},
    )
    assert r.status_code == 422, "EventIngest.event must be length-bounded"


def test_every_emitted_event_name_is_in_the_known_vocabulary():
    """The allowlist only works if it is COMPLETE: a broker-emitted name missing
    from KNOWN_EVENTS silently degrades to the 'other' bucket and its alert
    rate vanishes. Sweep the source for emit_event/_post_event literals."""
    import ast
    import pathlib

    import jobd
    from jobd.models import KNOWN_EVENTS

    src_root = pathlib.Path(jobd.__file__).parent
    emitted: set[str] = set()
    for py in src_root.rglob("*.py"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name in {"emit_event", "_emit_event", "_post_event"}:
                for arg in node.args[:2]:  # (logs_dir, event) or (client, event)
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        emitted.add(arg.value)
    unknown = emitted - KNOWN_EVENTS
    assert not unknown, (
        f"event names emitted in source but missing from models.KNOWN_EVENTS: "
        f"{sorted(unknown)} — their metrics silently collapse into 'other'"
    )
    assert len(emitted) >= 15, f"the AST sweep found too few emit sites ({emitted}) — broken?"
