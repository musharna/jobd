"""The CLI's exit status must follow the job's terminal STATE, not only exit_code.

`job wait`, `job submit --wait`, `job logs -f` and `job status` used to exit
`exit_code or 0`. Several terminal writers leave exit_code NULL — the sweeper's
`orphaned` (worker_died, wall_clock_exceeded), `scheduling_timeout`, a queued
cancel, `_honor_pending_cancel`, admission's `cwd_unreachable` FAILED, the
worker's drain-refuse `preempted` — so `job submit --wait ... && next` ran `next`
after a job that never succeeded.

These drive the real CLI commands against an in-process broker (the real /wait
SSE route and GET /jobs/{id}), with the row put in exactly the state those
writers leave behind.
"""

from __future__ import annotations

import pytest
from sqlalchemy import update
from typer.testing import CliRunner

import job_cli.cli as cli_mod
from jobd.client import JobdClient
from jobd.db import Job

_SUBMIT = {"cmd": ["bash", "-c", "true"], "cwd": "/tmp", "project": "project-a"}

# Every terminal state other than completed, as its writer leaves it: no exit_code.
_NULL_EXIT_TERMINALS = ["orphaned", "scheduling_timeout", "cancelled", "failed", "preempted"]


@pytest.fixture
def cli_on_broker(client, monkeypatch):
    """Point the CLI's JobdClient at the in-process broker."""

    class _SharedClient(JobdClient):
        def __exit__(self, *a) -> None:  # the TestClient outlives each command
            pass

    def _make() -> JobdClient:
        c = _SharedClient(base_url="http://testserver")
        c._client = client  # TestClient is an httpx.Client
        return c

    monkeypatch.setattr(cli_mod, "_client", _make)
    return client


def _job_in(client, state: str, exit_code: int | None) -> int:
    job_id = client.post("/submit", json=_SUBMIT).json()["id"]
    with client.app.state.engine.begin() as conn:
        conn.execute(update(Job).where(Job.id == job_id).values(state=state, exit_code=exit_code))
    return job_id


@pytest.mark.parametrize("command", [["wait"], ["logs", "-f"], ["status"]])
@pytest.mark.parametrize("state", _NULL_EXIT_TERMINALS)
def test_a_non_completed_terminal_with_no_exit_code_exits_nonzero(cli_on_broker, command, state):
    job_id = _job_in(cli_on_broker, state, None)
    r = CliRunner().invoke(cli_mod.app, [*command, str(job_id)])
    assert r.exit_code != 0, (
        f"`job {' '.join(command)}` exited 0 for a job that ended {state!r} with no "
        "exit_code — `job submit --wait ... && next` would run `next` after a job "
        f"that never succeeded. output: {r.output!r}"
    )


@pytest.mark.parametrize("command", [["wait"], ["logs", "-f"], ["status"]])
def test_completed_exits_zero_and_a_real_exit_code_passes_through(cli_on_broker, command):
    """Positive control in the same harness: success is still 0, and a real
    nonzero exit_code is still the CLI's exit status."""
    ok = _job_in(cli_on_broker, "completed", 0)
    assert CliRunner().invoke(cli_mod.app, [*command, str(ok)]).exit_code == 0
    failed = _job_in(cli_on_broker, "failed", 3)
    assert CliRunner().invoke(cli_mod.app, [*command, str(failed)]).exit_code == 3


def test_status_of_a_non_terminal_job_still_exits_zero(cli_on_broker):
    """`job status` on a live job is a read, not a verdict."""
    job_id = _job_in(cli_on_broker, "running", None)
    assert CliRunner().invoke(cli_mod.app, ["status", str(job_id)]).exit_code == 0


def test_a_log_line_that_looks_like_a_verdict_is_not_one(cli_on_broker, tmp_path):
    """Only the `terminal` SSE event carries the verdict. A workload printing
    JSON with a `state` key (e.g. `job status` output in its log) must not end
    the wait with an exit status read out of its own output."""
    from jobd.broker.joblog import job_log_path

    job_id = _job_in(cli_on_broker, "failed", 3)
    log = job_log_path(tmp_path / "logs", job_id)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"state": "completed", "exit_code": 0}\n')
    r = CliRunner().invoke(cli_mod.app, ["wait", str(job_id)])
    assert r.exit_code == 3, f"the log line was taken for the verdict: {r.output!r}"


def test_exit_for_refuses_a_non_terminal_state():
    with pytest.raises(ValueError):
        cli_mod.exit_for("running", None)


def test_exit_for_never_wraps_a_failure_to_zero():
    assert cli_mod.exit_for("failed", 256) == 1  # sys.exit(256) reads as 0 in a shell
    assert cli_mod.exit_for("completed", None) == 0


def test_a_signal_death_exits_128_plus_the_signal():
    """The worker records a signal death as Popen's negative returncode. Passed
    straight to sys.exit, -15 read as 241 in the shell; the convention every
    shell and `timeout` use is 128+N, so `$? -eq 143` means SIGTERM."""
    assert cli_mod.exit_for("cancelled", -15) == 143
    assert cli_mod.exit_for("failed", -9) == 137
    assert cli_mod.exit_for("failed", 3) == 3  # a positive exit code still passes through


def _array_on_broker(cli_on_broker, monkeypatch, outcomes: list[tuple[str, int | None]]):
    """Make the in-process broker's array members end as `outcomes` the moment
    /submit returns, so `job submit --count N --wait` streams real terminal events."""
    real_post = JobdClient.post

    def post(self, path, *, json=None, params=None):
        r = real_post(self, path, json=json, params=params)
        if path == "/submit" and "job_ids" in r.json():
            with cli_on_broker.app.state.engine.begin() as conn:
                for jid, (state, code) in zip(r.json()["job_ids"], outcomes, strict=True):
                    conn.execute(
                        update(Job).where(Job.id == jid).values(state=state, exit_code=code)
                    )
        return r

    monkeypatch.setattr(JobdClient, "post", post)


def test_array_wait_waits_on_every_member_and_fails_if_any_did(cli_on_broker, monkeypatch):
    """`job submit --count N --wait` exited from inside the FIRST member's wait
    (the shared wait helper calls sys.exit on its terminal event), so members 1..N-1
    were never waited on and their outcome never reached the exit status:
    `job submit --count 3 --wait ... && next` ran `next` with member 1 failed."""
    _array_on_broker(
        cli_on_broker, monkeypatch, [("completed", 0), ("failed", None), ("completed", 0)]
    )
    r = CliRunner().invoke(
        cli_mod.app,
        ["submit", "-p", "project-a", "--cwd", "/tmp", "--count", "3", "--wait", "--", "true"],
    )
    assert r.output.count("[terminal]") == 3, f"not every member was waited on: {r.output!r}"
    assert r.exit_code == 1, f"a failed member did not reach the exit status: {r.output!r}"


def test_array_wait_exits_zero_when_every_member_completed(cli_on_broker, monkeypatch):
    """Positive control in the same harness."""
    _array_on_broker(cli_on_broker, monkeypatch, [("completed", 0)] * 3)
    r = CliRunner().invoke(
        cli_mod.app,
        ["submit", "-p", "project-a", "--cwd", "/tmp", "--count", "3", "--wait", "--", "true"],
    )
    assert r.output.count("[terminal]") == 3, r.output
    assert r.exit_code == 0, r.output
