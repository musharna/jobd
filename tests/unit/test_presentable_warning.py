"""A resolved prediction must not be presented as a current warning.

Audit 2026-07-25 O-1, found in live output: job 3038 had been `completed` for
eleven hours and `GET /jobs/3038` still returned
`warning='will queue behind job 3037 on laptop'`, which `job list` rendered as
a present-tense ⚠.

`warning` is one column serving two purposes — durable explanations of a
terminal state (why CANCELLED, why PREEMPTED) and transient advisories about a
queue the job is sitting in. Once a job leaves the queue the second kind is
resolved by definition, but nothing cleared it: the sweeper's clearing pass
only visits QUEUED rows.

Suppressed in `_to_info` rather than at each terminal transition or in the CLI,
because every read surface converges there. Fixing one renderer would leave the
others wrong — which was already the symptom, since `job status` omitted the
warning while `job list` showed it.
"""

from __future__ import annotations

import pytest

from jobd.broker.constants import (
    _AUTO_PREEMPT_WARNING_PREFIX,
    _BLOCKED_WARNING_PREFIX,
    _PARENT_FAILED_WARNING_PREFIX,
    _UNMATCHEABLE_WARNING_PREFIX,
)
from jobd.broker.jobinfo import _presentable_warning
from jobd.models import JobState

_PREDICTION = "will queue behind job 3037 on laptop"
_PROJECT_ADVISORY = "project 'x' has no entry in projects.yaml; using global defaults"


class _Row:
    """Minimal stand-in: _presentable_warning reads only state and warning."""

    def __init__(self, state: str, warning: str | None):
        self.state = state
        self.warning = warning


@pytest.mark.parametrize(
    "terminal",
    [s.value for s in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED)],
)
@pytest.mark.parametrize("stale", [_PREDICTION, _PROJECT_ADVISORY])
def test_queue_advisories_are_not_shown_on_a_finished_job(terminal, stale):
    assert _presentable_warning(_Row(terminal, stale)) is None, (
        f"a resolved queue condition was presented as a current warning on a {terminal} job"
    )


@pytest.mark.parametrize("live", [JobState.QUEUED.value, JobState.RUNNING.value])
@pytest.mark.parametrize("advisory", [_PREDICTION, _PROJECT_ADVISORY])
def test_queue_advisories_still_show_while_the_job_is_live(live, advisory):
    """The prediction is only stale once the job has LEFT the queue. While it is
    still waiting, it is the whole point."""
    assert _presentable_warning(_Row(live, advisory)) == advisory


@pytest.mark.parametrize(
    "durable",
    [
        f"{_PARENT_FAILED_WARNING_PREFIX}42 → failed",
        f"{_AUTO_PREEMPT_WARNING_PREFIX}99",
    ],
)
def test_warnings_that_explain_the_terminal_state_survive(durable):
    """These are the reason the job is in that state — dropping them would lose
    the only explanation of why a job is CANCELLED or PREEMPTED."""
    assert _presentable_warning(_Row(JobState.CANCELLED.value, durable)) == durable


@pytest.mark.parametrize(
    "queue_state_warning", [_BLOCKED_WARNING_PREFIX, _UNMATCHEABLE_WARNING_PREFIX]
)
def test_the_other_queue_warnings_are_also_suppressed_when_terminal(queue_state_warning):
    """`blocked:` and `no matching worker —` describe the queue too. The sweeper
    clears them, but only on rows that are still QUEUED — so a job that reached a
    terminal state while carrying one kept it forever."""
    row = _Row(JobState.COMPLETED.value, f"{queue_state_warning}something")
    assert _presentable_warning(row) is None


def test_no_warning_stays_no_warning():
    assert _presentable_warning(_Row(JobState.COMPLETED.value, None)) is None
    assert _presentable_warning(_Row(JobState.QUEUED.value, "")) is None


def test_the_api_stops_serving_the_stale_prediction_end_to_end(client):
    """Through the real endpoints, not just the helper — a unit test alone would
    not catch `_to_info` failing to call it. Reproduces the live shape: a
    completed job still carrying a submit-time queue prediction."""
    from datetime import datetime

    from sqlalchemy import insert, select

    from jobd.db import Job

    with client.app.state.engine.begin() as conn:
        conn.execute(
            insert(Job).values(
                project="p",
                priority=50,
                cmd_json='["echo","hi"]',
                cwd="/tmp",
                state=JobState.COMPLETED.value,
                warning=_PREDICTION,
                submitted_at=datetime(2026, 7, 24, 19, 0),
                finished_at=datetime(2026, 7, 24, 19, 21),
            )
        )
        job_id = conn.execute(select(Job.id)).scalar_one()

    body = client.get(f"/jobs/{job_id}").json()
    assert body["state"] == JobState.COMPLETED.value
    assert body["warning"] is None, (
        f"GET /jobs/{{id}} still serves the resolved prediction: {body['warning']!r}"
    )

    listed = client.get("/jobs").json()
    rows = listed if isinstance(listed, list) else listed.get("jobs", [])
    assert all(r.get("warning") is None for r in rows), rows
