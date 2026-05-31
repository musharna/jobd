"""Worker-side _post_event helper: envelope shape, error swallowing."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent / "worker"))

from job_worker import _post_event  # noqa: E402


def test_post_event_builds_envelope_for_worker_shutdown():
    client = MagicMock()
    _post_event(client, "worker_shutdown", host="laptop")
    client.post.assert_called_once()
    args, kwargs = client.post.call_args
    assert args[0] == "/events"
    assert kwargs["timeout"] == 2.0
    body = kwargs["json"]
    assert body["source"] == "worker"
    assert body["event"] == "worker_shutdown"
    assert body["payload"] == {"host": "laptop"}
    assert "job_id" not in body
    assert "project" not in body


def test_post_event_includes_job_id_and_project_when_set():
    client = MagicMock()
    _post_event(
        client,
        "watchdog_fired",
        job_id=123,
        project="project-b",
        host="desktop",
        reason="idle_timeout",
        threshold_s=600,
    )
    body = client.post.call_args.kwargs["json"]
    assert body["job_id"] == 123
    assert body["project"] == "project-b"
    assert body["payload"] == {"host": "desktop", "reason": "idle_timeout", "threshold_s": 600}


def test_post_event_swallows_network_error_logs_to_stderr(capsys):
    client = MagicMock()
    client.post.side_effect = httpx.ConnectError("broker unreachable")

    _post_event(client, "worker_shutdown", host="laptop")

    captured = capsys.readouterr()
    assert "/events POST failed" in captured.err
    assert "worker_shutdown" in captured.err
    # No exception propagated.


def test_post_event_swallows_http_error_response_no_raise():
    """httpx.Client.post does not raise_for_status; even a 4xx response is silent.
    The helper does not call raise_for_status itself, so the worker hot path
    cannot blow up on a malformed audit event."""
    client = MagicMock()
    response = MagicMock()
    response.status_code = 422
    client.post.return_value = response

    _post_event(client, "watchdog_fired", host="laptop", reason="x", threshold_s=1)
    # No exception means pass.


def test_post_event_is_called_at_three_trigger_sites_in_source():
    """Sentinel: confirm wiring landed at watchdog wall-timeout, watchdog
    idle-timeout, and shutdown handler. If a refactor removes one of these,
    this test fails loud rather than silently dropping audit coverage."""
    src = (Path(__file__).parent.parent / "worker" / "job_worker.py").read_text()
    assert src.count("_post_event(") >= 4, (
        "expected at least 4 _post_event calls (helper def + 3 trigger sites); "
        f"found {src.count('_post_event(')}"
    )
    # Specific content checks: each of the three reasons/events should appear
    # alongside _post_event in the source.
    assert '"watchdog_fired"' in src
    assert '"wall_timeout"' in src
    assert '"idle_timeout"' in src
    assert '"worker_shutdown"' in src
