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
