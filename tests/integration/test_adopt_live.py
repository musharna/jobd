"""Real-execution check for process adoption (docs/adoption.md).

A REAL broker process, a REAL worker process, the REAL `job` CLI, and a real
`sleep 30` that none of them started. Everything the feature promises lives on
boundaries a TestClient cannot reach: /proc of a foreign pid, a pidfd, the
heartbeat response as a delivery channel, a signal to a non-child.

    JOBD_LIVE=1 pytest tests/integration/test_adopt_live.py -v

Self-contained: own broker on a free port, own temp DB/config/logs. It never
talks to the broker at $JOBD_URL.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from jobd import procident
from tests.integration.test_broker_concurrency_live import (
    _PY,
    _Broker,
    _submit,
    _terminate,
    _wait_state,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("JOBD_LIVE") != "1",
        reason="Set JOBD_LIVE=1 to run the live broker+worker integration harness",
    ),
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="adoption is Linux-only"),
]

HOST = "adopt-live-w1"


@pytest.fixture(scope="module")
def broker() -> Iterator[_Broker]:
    b = _Broker(Path(tempfile.mkdtemp(prefix="jobd-adopt-live-")))
    try:
        yield b
    finally:
        b.stop()


@pytest.fixture(scope="module")
def worker(broker: _Broker) -> Iterator[subprocess.Popen]:
    # A jobd worker's first act is `_sweep_stale_scopes`: SIGKILL every
    # `jobd-<id>.scope` in this user's systemd session, on the premise of one
    # worker per session. On a machine that ALSO runs a real worker, a test
    # worker would kill that worker's live jobs. So this one gets a PATH holding
    # nothing but `true`: no `systemctl` to sweep with, no `systemd-run` to
    # create colliding `jobd-1.scope` units with.
    bindir = broker.tmp / "bin"
    bindir.mkdir()
    true_path = shutil.which("true")
    assert true_path is not None
    (bindir / "true").symlink_to(true_path)
    env = dict(broker.env, JOBD_URL=broker.base, JOBD_WORKER_HOST=HOST, PATH=str(bindir))
    env.pop("JOBD_WORKER_MAX_CONCURRENT_JOBS", None)  # single slot: the point of test 1
    env["JOBD_WORKER_CHECKPOINT_ROOT"] = str(broker.tmp / "checkpoints")
    proc = subprocess.Popen(
        [_PY, "-c", "from jobd.worker.job_worker import main; main()"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not broker.get("/workers"):
        time.sleep(0.2)
    assert broker.get("/workers"), "worker never registered"
    try:
        yield proc
    finally:
        _terminate(proc)


@pytest.fixture
def sleeper() -> Iterator[subprocess.Popen]:
    proc = subprocess.Popen(["sleep", "30"])
    cmdline = Path(f"/proc/{proc.pid}/cmdline")
    while not cmdline.read_bytes().startswith(b"sleep\0"):  # fork -> exec
        time.sleep(0.01)
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def _job_cli(broker: _Broker, *args: str) -> subprocess.CompletedProcess:
    env = dict(broker.env, JOBD_URL=broker.base, JOBD_WORKER_HOST=HOST)
    return subprocess.run(
        [_PY, "-c", "from job_cli.cli import main; main()", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _worker_row(broker: _Broker) -> dict:
    (row,) = [w for w in broker.get("/workers") if w["host"] == HOST]
    return row


def test_adopted_process_is_running_while_it_lives_and_holds_the_slot(
    broker: _Broker, worker: subprocess.Popen, sleeper: subprocess.Popen
) -> None:
    r = _job_cli(broker, "adopt", "--pid", str(sleeper.pid), "--project", "project-a")
    assert r.returncode == 0, r.stderr
    job = json.loads(r.stdout)
    jid = job["id"]
    assert job["state"] == "running" and job["cmd"] == ["sleep", "30"]

    # The "queue behind the thing I already started" case: a single-slot worker
    # must not start this while the adopted process holds the slot.
    behind = _submit(broker, ["true"], fast_path=True)

    # The worker (not just the broker's row) has taken the watch.
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and _worker_row(broker)["running"] != 1:
        time.sleep(0.25)
    assert _worker_row(broker)["running"] == 1, _worker_row(broker)
    time.sleep(6)  # > one heartbeat + one reconcile look
    assert sleeper.poll() is None
    assert broker.get(f"/jobs/{jid}")["state"] == "running"
    assert broker.get(f"/jobs/{behind}")["state"] == "queued"

    sleeper.kill()  # ends by someone else's hand
    sleeper.wait(timeout=10)
    final = _wait_state(broker, jid, {"orphaned", "completed", "failed", "cancelled"}, 20)
    assert (final["state"], final["exit_code"], final["termination_reason"]) == (
        "orphaned",
        None,
        "adopted_exit_unobserved",
    ), final
    # slot released: the queued job now runs, by the normal launch path
    assert _wait_state(broker, behind, {"completed", "failed"}, 30)["state"] == "completed"
    assert broker.get(f"/jobs/{jid}/output")["size_bytes"] == 0  # no logs, as documented


def test_wrong_start_time_is_refused_then_the_true_identity_adopts_and_cancels(
    broker: _Broker, worker: subprocess.Popen, sleeper: subprocess.Popen
) -> None:
    ident = procident.read_identity(sleeper.pid)
    body = {
        "pid": ident.pid,
        "host": HOST,
        "project": "project-a",
        "cmd": ident.cmd,
        "cwd": ident.cwd,
    }
    # A pid whose start time is not the one recorded = a recycled pid.
    bad = broker.post("/adopt", {**body, "start_ticks": ident.start_ticks + 1})["id"]
    final = _wait_state(broker, bad, {"failed", "orphaned", "completed", "cancelled"}, 20)
    assert (final["state"], final["termination_reason"]) == ("failed", "adopt_pid_mismatch")
    assert sleeper.poll() is None, "a refused adoption must not touch the process"

    # positive control: same pid, same harness, true start time -> watched
    good = broker.post("/adopt", {**body, "start_ticks": ident.start_ticks})["id"]
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and _worker_row(broker)["running"] != 1:
        time.sleep(0.25)
    assert _worker_row(broker)["running"] == 1
    assert broker.get(f"/jobs/{good}")["state"] == "running"

    assert _job_cli(broker, "cancel", str(good)).returncode == 0
    assert sleeper.wait(timeout=20) == -signal.SIGTERM
    assert _wait_state(broker, good, {"cancelled", "orphaned", "failed"}, 20)["state"] == (
        "cancelled"
    )
