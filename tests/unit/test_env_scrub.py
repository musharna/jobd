"""Env-at-rest scrub (JOBD_ENV_SCRUB_HOURS): terminal jobs' env_json VALUES are
masked by the sweeper once past the grace window.

Rows are kept forever by default (JOB_RETENTION_DAYS_DEFAULT=0), so a secret
submitted via `env` otherwise sits plaintext in the SQLite file — and every
backup of it — indefinitely. Nothing observable is lost (every read surface
already masks values); the one hazard is the terminal→QUEUED restore path,
pinned below: a cascade-cancelled child a parent resurrect can restore must
keep its REAL env, or the restored job would run with "***" values.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from jobd.app import build_app
from jobd.db import Job


@pytest.fixture
def client_logs(tmp_path, sample_projects_yaml, sample_profiles_yaml, sample_classifier_yaml):
    app = build_app(
        db_url=f"sqlite:///{tmp_path}/jobd.db",
        projects_path=sample_projects_yaml,
        profiles_path=sample_profiles_yaml,
        classifier_path=sample_classifier_yaml,
        logs_path=tmp_path / "logs",
    )
    return TestClient(app), tmp_path / "logs"


def _submit(client: TestClient, env: dict[str, str] | None = None) -> int:
    r = client.post(
        "/submit",
        json={
            "project": "project-b",
            "cmd": ["true"],
            "cwd": "/tmp/foo",
            "host_pin": "any",
            "env": (
                env
                if env is not None
                else {"HF_TOKEN": "hf_secret123", "WANDB_API_KEY": "wb_secret"}
            ),
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _force(
    client: TestClient,
    job_id: int,
    *,
    state: str,
    finished_hours_ago: float | None,
    warning: str | None = None,
) -> None:
    finished = (
        None
        if finished_hours_ago is None
        else datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=finished_hours_ago)
    )
    values: dict = {"state": state, "finished_at": finished}
    if warning is not None:
        values["warning"] = warning
    with client.app.state.engine.begin() as conn:
        conn.execute(update(Job).where(Job.id == job_id).values(**values))


def _row(client: TestClient, job_id: int) -> tuple[str, object]:
    with client.app.state.engine.begin() as conn:
        return conn.execute(select(Job.env_json, Job.env_scrubbed_at).where(Job.id == job_id)).one()


def _scrub_events(logs_dir) -> list[dict]:
    f = logs_dir / "events.jsonl"
    if not f.exists():
        return []
    rows = [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("event") == "env_scrubbed"]


def test_scrubs_old_terminal_job_keys_kept(client_logs, monkeypatch):
    monkeypatch.delenv("JOBD_ENV_SCRUB_HOURS", raising=False)
    client, logs = client_logs
    jid = _submit(client)
    _force(client, jid, state="completed", finished_hours_ago=2)

    assert client.app.state.scrub_terminal_env() == 1
    env_json, stamped = _row(client, jid)
    assert json.loads(env_json) == {"HF_TOKEN": "***", "WANDB_API_KEY": "***"}
    assert stamped is not None
    assert "hf_secret123" not in env_json

    events = _scrub_events(logs)
    assert len(events) == 1 and events[0]["payload"]["count"] == 1

    # Idempotent: the stamped row is never re-examined, no duplicate event.
    assert client.app.state.scrub_terminal_env() == 0
    assert len(_scrub_events(logs)) == 1


def test_scrubbed_row_reads_same_as_before(client_logs, monkeypatch):
    """The scrub changes what's ON DISK, not what any API returns: reads were
    always masked, so GET /jobs/{id} is byte-identical across the scrub."""
    monkeypatch.delenv("JOBD_ENV_SCRUB_HOURS", raising=False)
    client, _logs = client_logs
    jid = _submit(client)
    _force(client, jid, state="completed", finished_hours_ago=2)

    before = client.get(f"/jobs/{jid}").json()["env"]
    assert client.app.state.scrub_terminal_env() == 1
    after = client.get(f"/jobs/{jid}").json()["env"]
    assert before == after == {"HF_TOKEN": "***", "WANDB_API_KEY": "***"}


def test_fresh_terminal_and_active_jobs_untouched(client_logs, monkeypatch):
    monkeypatch.delenv("JOBD_ENV_SCRUB_HOURS", raising=False)
    client, _logs = client_logs
    fresh = _submit(client)
    _force(client, fresh, state="completed", finished_hours_ago=0.5)  # inside 1h grace
    queued = _submit(client)  # QUEUED — env must stay real for the future claim

    assert client.app.state.scrub_terminal_env() == 0
    assert "hf_secret123" in _row(client, fresh)[0]
    assert "hf_secret123" in _row(client, queued)[0]


def test_cascade_restorable_child_is_never_scrubbed(client_logs, monkeypatch):
    """THE hazard case: a cascade-cancelled child (warning `parent_failed: …`)
    can be restored to QUEUED by its parent's resurrect and re-claimed — the
    claim delivers env_json, so scrubbing it would run the job with "***"
    values. It must be excluded no matter how old."""
    monkeypatch.delenv("JOBD_ENV_SCRUB_HOURS", raising=False)
    client, _logs = client_logs
    child = _submit(client)
    _force(
        client,
        child,
        state="cancelled",
        finished_hours_ago=500,
        warning="parent_failed: 123 → orphaned",
    )

    assert client.app.state.scrub_terminal_env() == 0
    env_json, stamped = _row(client, child)
    assert json.loads(env_json)["HF_TOKEN"] == "hf_secret123"
    assert stamped is None

    # A HUMAN-cancelled job (no parent_failed stamp) of the same age IS scrubbed
    # — nothing restores it.
    human = _submit(client)
    _force(client, human, state="cancelled", finished_hours_ago=500)
    assert client.app.state.scrub_terminal_env() == 1
    assert json.loads(_row(client, human)[0])["HF_TOKEN"] == "***"


def test_negative_setting_disables(client_logs, monkeypatch):
    monkeypatch.setenv("JOBD_ENV_SCRUB_HOURS", "-1")
    client, _logs = client_logs
    jid = _submit(client)
    _force(client, jid, state="completed", finished_hours_ago=1000)
    assert client.app.state.scrub_terminal_env() == 0
    assert "hf_secret123" in _row(client, jid)[0]


def test_zero_means_scrub_at_next_sweep(client_logs, monkeypatch):
    monkeypatch.setenv("JOBD_ENV_SCRUB_HOURS", "0")
    client, _logs = client_logs
    jid = _submit(client)
    _force(client, jid, state="completed", finished_hours_ago=0.01)
    assert client.app.state.scrub_terminal_env() == 1


def test_invalid_setting_falls_back_to_default(client_logs, monkeypatch):
    monkeypatch.setenv("JOBD_ENV_SCRUB_HOURS", "garbage")
    client, _logs = client_logs
    jid = _submit(client)
    _force(client, jid, state="completed", finished_hours_ago=2)
    assert client.app.state.scrub_terminal_env() == 1


def test_empty_env_rows_are_skipped_entirely(client_logs, monkeypatch):
    """A job submitted without env ({}) is not selected, not stamped, and
    emits nothing — the common case must stay a no-op."""
    monkeypatch.delenv("JOBD_ENV_SCRUB_HOURS", raising=False)
    client, logs = client_logs
    jid = _submit(client, env={})
    _force(client, jid, state="completed", finished_hours_ago=2)
    assert client.app.state.scrub_terminal_env() == 0
    assert _row(client, jid)[1] is None
    assert _scrub_events(logs) == []


def test_malformed_env_json_is_dropped_not_retried(client_logs, monkeypatch):
    monkeypatch.delenv("JOBD_ENV_SCRUB_HOURS", raising=False)
    client, _logs = client_logs
    jid = _submit(client)
    _force(client, jid, state="failed", finished_hours_ago=2)
    with client.app.state.engine.begin() as conn:
        conn.execute(update(Job).where(Job.id == jid).values(env_json="{not json"))

    assert client.app.state.scrub_terminal_env() == 1
    env_json, stamped = _row(client, jid)
    assert env_json == "{}" and stamped is not None
    # And it is settled — never re-examined.
    assert client.app.state.scrub_terminal_env() == 0


def test_sweep_once_runs_the_scrub(client_logs, monkeypatch):
    """The pass is wired into the sweep, not just exposed as a seam."""
    monkeypatch.delenv("JOBD_ENV_SCRUB_HOURS", raising=False)
    client, _logs = client_logs
    jid = _submit(client)
    _force(client, jid, state="completed", finished_hours_ago=2)
    client.app.state.sweep_once()
    assert json.loads(_row(client, jid)[0])["HF_TOKEN"] == "***"
