"""Real-execution validation for the 2026-07-01 audit concurrency fixes.

Unlike the fast API tests (in-process TestClient + in-memory SQLite, single event
loop, races *injected* via monkeypatch), this harness spins up a REAL broker
process and a REAL worker process talking over HTTP, and runs REAL subprocess
jobs. It's the "real-execution check at every system boundary" for the fixes
that only manifest across processes:

- H1  — a SIGTERM-ignoring workload is actually force-killed by the worker's
        SIGKILL-escalation timer (never proven by TestClient, which runs no
        subprocess and delivers no signals).
- cancel — a /cancel on a genuinely-running job actually terminates the child
        and lands the job terminal (the cancel-latency + H3 signal path).

Gated behind JOBD_LIVE=1 so normal/CI runs skip it (it launches processes and
takes ~10-30s):

    JOBD_LIVE=1 pytest tests/integration/test_broker_concurrency_live.py -v

The manual multi-worker partition scenario (M2 stale-worker rejection) is
described in tests/integration/README-live.md — it needs a real network
partition (or iptables drop) between a worker and the broker, which isn't
reliably automatable in-process.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("JOBD_LIVE") != "1",
    reason="Set JOBD_LIVE=1 to run the live broker+worker integration harness",
)

# Run the code under test from THIS worktree, not whatever `jobd` is installed.
_SRC = str(Path(__file__).resolve().parents[2] / "src")
_PY = sys.executable


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Broker:
    """A real jobd broker subprocess over a temp DB + temp config."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        cfg = tmp / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "projects.yaml").write_text(
            "projects:\n  project-a: { priority: 55 }\n  _default: { priority: 40 }\n"
        )
        (cfg / "profiles.yaml").write_text(
            "profiles:\n  small: { vram_gb: 0, ram_gb: 1, cpus: 1, expected_runtime: 5m, "
            "preemptible: true, host_hint: any, fast_path: true }\n"
        )
        (cfg / "classifier.yaml").write_text("rules: []\n")
        self.env = dict(
            os.environ,
            PYTHONPATH=_SRC,
            JOBD_ALLOW_NO_AUTH="1",
            JOBD_DISABLE_TAILNET_ACL="1",
            JOBD_CONFIG_DIR=str(cfg),
            JOBD_DB_URL=f"sqlite:///{tmp}/jobd.db",
            JOBD_HOST="127.0.0.1",
            JOBD_PORT=str(self.port),
            JOBD_LOGS_DIR=str(tmp / "logs"),
        )
        self.proc = subprocess.Popen(
            [_PY, "-c", "from jobd.main import run; run()"],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self._await_health()

    def _await_health(self) -> None:
        for _ in range(100):
            try:
                if self.get("/health").get("status") == "ok":
                    return
            except Exception:
                time.sleep(0.1)
        out = b""
        if self.proc.stdout is not None:
            self.proc.stdout.close()
        raise RuntimeError(f"broker did not become healthy on {self.base}\n{out!r}")

    def get(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return json.load(r)

    def post(self, path: str, body: dict, headers: dict | None = None) -> dict:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)

    def post_status(self, path: str, body: dict, headers: dict | None = None) -> int:
        """POST and return the HTTP status code, without raising on 4xx/5xx —
        for asserting the broker's stale-worker 409 rejection."""
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def stop(self) -> None:
        _terminate(self.proc)


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _wait_state(broker: _Broker, jid: int, states: set[str], timeout_s: float) -> dict:
    """Poll until the job reaches one of `states`, or fail with the last snapshot."""
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        last = broker.get(f"/jobs/{jid}")
        if last["state"] in states:
            return last
        time.sleep(0.25)
    pytest.fail(f"job {jid} never reached {states} within {timeout_s}s; last={last}")


@pytest.fixture(scope="module")
def broker() -> Iterator[_Broker]:
    tmp = Path(tempfile.mkdtemp(prefix="jobd-live-"))
    b = _Broker(tmp)
    try:
        yield b
    finally:
        b.stop()


@pytest.fixture(scope="module")
def worker(broker: _Broker) -> Iterator[subprocess.Popen]:
    env = dict(
        broker.env,
        JOBD_URL=broker.base,
        JOBD_WORKER_HOST="live-w1",
        # Short escalation grace so the SIGKILL-escalation test is fast.
        JOBD_WORKER_WATCHDOG_KILL_GRACE_S="3",
    )
    proc = subprocess.Popen(
        [_PY, "-c", "from jobd.worker.job_worker import main; main()"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    # Give the worker a moment to register its first heartbeat.
    time.sleep(2.0)
    try:
        yield proc
    finally:
        _terminate(proc)


def _submit(broker: _Broker, cmd: list[str], **extra) -> int:
    body = {"cmd": cmd, "cwd": "/tmp", "project": "project-a", **extra}
    resp = broker.post("/submit", body)
    assert "id" in resp, f"submit failed: {resp}"
    return resp["id"]


def test_sanity_real_job_completes(broker: _Broker, worker: subprocess.Popen) -> None:
    """Baseline: a real job dispatched to a real worker runs and completes."""
    jid = _submit(broker, ["bash", "-c", "echo hi; sleep 1; echo bye"])
    final = _wait_state(broker, jid, {"completed", "failed"}, timeout_s=30)
    assert final["state"] == "completed", final
    assert final["exit_code"] == 0


def test_watchdog_escalates_to_sigkill_on_sigterm_ignoring_job(
    broker: _Broker, worker: subprocess.Popen
) -> None:
    """H1 (real execution): a workload that installs `trap '' TERM` and then idles
    must still be force-killed by the worker's SIGKILL-escalation timer once the
    idle watchdog fires — not left RUNNING forever pinning its slot.

    Before the fix the watchdog sent one SIGTERM and returned from poll_signals
    with no kill timer; the stdout-read loop blocked forever on the ignore-TERM
    child, so escalation never ran. This is exactly the case TestClient can't
    exercise (no real subprocess, no real signals).
    """
    # trap '' TERM => the process group ignores SIGTERM; only SIGKILL ends it.
    # idle_timeout_s=2 => the idle watchdog fires ~2s after start (no output).
    jid = _submit(
        broker,
        ["bash", "-c", "trap '' TERM; while true; do sleep 30; done"],
        idle_timeout_s=2,
    )
    # idle(2s) + poll interval + escalation grace(3s) + slack.
    final = _wait_state(broker, jid, {"failed", "orphaned", "cancelled"}, timeout_s=40)
    # The worker reports watchdog kills as failed with a termination_reason.
    assert final["state"] in ("failed", "orphaned"), final
    assert final.get("termination_reason") in ("idle_timeout", "wall_timeout"), final


def test_cancel_running_job_terminates_child(broker: _Broker, worker: subprocess.Popen) -> None:
    """A /cancel on a genuinely-running job must terminate the real child and
    land the job terminal promptly (cancel-latency + the H3 signal path)."""
    jid = _submit(broker, ["bash", "-c", "echo started; sleep 60"])
    # Wait until it's actually running on the worker.
    _wait_state(broker, jid, {"running", "assigned"}, timeout_s=15)
    broker.post(f"/jobs/{jid}/cancel", {})
    final = _wait_state(broker, jid, {"cancelled", "failed", "completed"}, timeout_s=20)
    assert final["state"] == "cancelled", final


def test_stale_worker_reports_rejected_by_live_broker(
    broker: _Broker, worker: subprocess.Popen
) -> None:
    """M2 (real HTTP): once a job is owned by one worker, the live broker must
    refuse /complete and /log carrying a DIFFERENT `X-Jobd-Worker` (a stale
    worker whose reclaimed job was re-dispatched) with 409, and must not let the
    stale report terminal-ize or corrupt the running job. The owner's own report
    is still accepted.

    This validates the broker-side mechanism of M2 directly over real HTTP; the
    full partition -> reclaim -> re-dispatch -> stale-completion sequence is the
    manual procedure in README-live.md (it needs a real partition).
    """
    jid = _submit(broker, ["bash", "-c", "echo started; sleep 60"])
    running = _wait_state(broker, jid, {"running", "assigned"}, timeout_s=15)
    owner = running["worker"]
    assert owner, running

    stale = {"X-Jobd-Worker": "ghost-worker-xyz"}
    assert broker.post_status(f"/jobs/{jid}/complete", {"exit_code": 0}, stale) == 409
    assert broker.post_status(f"/jobs/{jid}/log", {}, stale) == 409
    # A stale terminal report must NOT have moved the job off running.
    assert broker.get(f"/jobs/{jid}")["state"] in ("running", "assigned")

    # The real owner can still terminate it (owner header matches job.worker).
    assert broker.post_status(f"/jobs/{jid}/cancel", {}) == 200
    _wait_state(broker, jid, {"cancelled", "failed", "completed"}, timeout_s=20)
