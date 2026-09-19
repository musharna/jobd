"""Adoption of an already-running process (docs/adoption.md).

The watcher tests use REAL processes — a foreign PID is the boundary this
feature lives on, and a mocked one would encode my belief about /proc and
pidfds rather than test it. Every refusal asserted here carries its positive
control in the same test: the same harness, given the right identity, adopts.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import psutil
import pytest
from typer.testing import CliRunner

from jobd import procident
from jobd.worker import adopt, job_worker

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="adoption is Linux-only (/proc)"
)

HB = {"free_vram_gb": 0, "unregistered_vram_gb": 0, "free_ram_gb": 8, "idle_cpus": 4}


# --------------------------------------------------------------------- helpers


def _until(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _spawn_sleep(**kw) -> subprocess.Popen:
    """`sleep 30`, returned only once it has exec'd: between fork and exec the
    child's /proc cmdline is not yet sleep's."""
    proc = subprocess.Popen(["sleep", "30"], **kw)
    cmdline = Path(f"/proc/{proc.pid}/cmdline")
    assert _until(lambda: cmdline.read_bytes().startswith(b"sleep\0")), "never exec'd"
    return proc


@pytest.fixture
def sleeper():
    """A real process this test did not fork *through jobd*: `sleep 30`."""
    proc = _spawn_sleep()
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def _ticks(pid: int) -> int:
    st = procident.read_stat(pid)
    assert st is not None
    return st.start_ticks


def _watch_in_thread(pid, ticks, **kw):
    box: dict = {}

    def run():
        try:
            box["outcome"] = adopt.watch(pid, ticks, **kw)
        except Exception as e:  # surfaced to the asserting thread, not swallowed
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, box


def _kw(**over):
    base = dict(poll_signal=lambda: None, stop=threading.Event(), grace_s=30.0, interval_s=0.05)
    base.update(over)
    return base


# ------------------------------------------------------------------- procident


def test_parse_stat_survives_a_comm_with_spaces_and_parens():
    tail = " ".join(["S"] + [str(n) for n in range(4, 22)] + ["987654", "0"])
    st = procident.parse_stat(f"4242 (evil) name (x) {tail}")
    assert (st.state, st.start_ticks) == ("S", 987654)
    with pytest.raises(ValueError, match="unparseable"):
        procident.parse_stat("4242 (short) S 1 2 3")


def test_start_ticks_agree_with_an_independent_reading_of_the_kernel(sleeper):
    """psutil derives create_time from the same kernel field by its own parser. If
    field 22 were mis-indexed here, the two would disagree by far more than 1s."""
    st = procident.read_stat(sleeper.pid)
    assert st is not None and st.state != "Z"
    derived = psutil.boot_time() + st.start_ticks / os.sysconf("SC_CLK_TCK")
    assert abs(derived - psutil.Process(sleeper.pid).create_time()) < 1.0


def test_read_identity_reports_the_real_command_and_cwd(tmp_path):
    proc = _spawn_sleep(cwd=tmp_path)
    try:
        ident = procident.read_identity(proc.pid)
        assert ident.cmd == ["sleep", "30"]
        assert ident.cwd == str(tmp_path)
        assert ident.start_ticks == _ticks(proc.pid)
    finally:
        proc.kill()
        proc.wait(timeout=10)
    # reaped: the pid no longer names anything
    assert procident.read_stat(proc.pid) is None
    with pytest.raises(ProcessLookupError, match="no such process"):
        procident.read_identity(proc.pid)


# --------------------------------------------------------------------- watcher


@pytest.mark.parametrize("use_pidfd", [True, False], ids=["pidfd", "proc-poll"])
def test_watch_blocks_while_the_process_lives_and_never_claims_success(sleeper, use_pidfd):
    t, box = _watch_in_thread(sleeper.pid, _ticks(sleeper.pid), **_kw(use_pidfd=use_pidfd))
    time.sleep(0.5)
    assert t.is_alive(), f"watch returned while the process was alive: {box}"
    sleeper.kill()  # it ends by someone else's hand; jobd cannot know how
    t.join(timeout=10)
    assert not t.is_alive() and "error" not in box, box
    assert box["outcome"] == adopt.AdoptOutcome("orphaned", "adopted_exit_unobserved")


@pytest.mark.parametrize("use_pidfd", [True, False], ids=["pidfd", "proc-poll"])
def test_a_pid_with_another_start_time_is_refused_and_left_alone(sleeper, use_pidfd):
    ticks = _ticks(sleeper.pid)
    with pytest.raises(adopt.AdoptRefused) as ei:
        adopt.watch(sleeper.pid, ticks + 1, **_kw(use_pidfd=use_pidfd))
    assert ei.value.reason == "adopt_pid_mismatch"
    assert sleeper.poll() is None, "a refused adoption must not touch the process"
    # positive control: the SAME pid with its true start time is adopted
    t, box = _watch_in_thread(sleeper.pid, ticks, **_kw(use_pidfd=use_pidfd))
    time.sleep(0.3)
    assert t.is_alive() and not box, box
    sleeper.kill()
    t.join(timeout=10)
    assert box["outcome"].final_state == "orphaned"


def test_an_exited_pid_is_refused_as_gone(sleeper):
    done = subprocess.Popen(["true"])
    done.wait(timeout=10)
    with pytest.raises(adopt.AdoptRefused) as ei:
        adopt.watch(done.pid, 1, **_kw())
    assert ei.value.reason == "adopt_pid_gone"
    # positive control: a live pid through the same call is not "gone"
    t, box = _watch_in_thread(sleeper.pid, _ticks(sleeper.pid), **_kw())
    time.sleep(0.3)
    assert t.is_alive() and not box, box
    sleeper.kill()
    t.join(timeout=10)


def test_a_process_owned_by_another_user_is_refused(sleeper, monkeypatch):
    ticks = _ticks(sleeper.pid)
    real = procident.read_owner_uid
    monkeypatch.setattr(procident, "read_owner_uid", lambda pid: real(pid) + 1)
    with pytest.raises(adopt.AdoptRefused) as ei:
        adopt.watch(sleeper.pid, ticks, **_kw())
    assert ei.value.reason == "adopt_uid_mismatch"
    assert sleeper.poll() is None
    monkeypatch.setattr(procident, "read_owner_uid", real)
    # positive control: our own process is adopted
    t, box = _watch_in_thread(sleeper.pid, ticks, **_kw())
    time.sleep(0.3)
    assert t.is_alive() and not box, box
    sleeper.kill()
    t.join(timeout=10)


@pytest.mark.parametrize("use_pidfd", [True, False], ids=["pidfd", "proc-poll"])
def test_cancel_sigterms_the_process_and_reports_cancelled(sleeper, use_pidfd):
    asked = threading.Event()

    def poll():
        return "cancel" if asked.is_set() else None

    t, box = _watch_in_thread(
        sleeper.pid, _ticks(sleeper.pid), **_kw(poll_signal=poll, use_pidfd=use_pidfd)
    )
    time.sleep(0.3)
    assert sleeper.poll() is None, "signalled before any cancel was requested"
    asked.set()
    assert sleeper.wait(timeout=10) == -signal.SIGTERM
    t.join(timeout=10)
    assert box["outcome"] == adopt.AdoptOutcome("cancelled", None)


def test_cancel_escalates_to_sigkill_when_sigterm_is_ignored():
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); time.sleep(30)",
        ],
        stdout=subprocess.PIPE,
    )
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == b"ready"
        t, box = _watch_in_thread(
            proc.pid, _ticks(proc.pid), **_kw(poll_signal=lambda: "cancel", grace_s=0.5)
        )
        assert proc.wait(timeout=15) == -signal.SIGKILL
        t.join(timeout=10)
        assert box["outcome"] == adopt.AdoptOutcome("cancelled", None)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def test_a_stopping_worker_leaves_the_process_alone_and_reports_nothing(sleeper):
    stop = threading.Event()
    t, box = _watch_in_thread(sleeper.pid, _ticks(sleeper.pid), **_kw(stop=stop))
    time.sleep(0.3)
    stop.set()
    t.join(timeout=10)
    assert box == {"outcome": None}
    assert sleeper.poll() is None


# ---------------------------------------------------------------------- broker


def _heartbeat(client, host="w1", tags=(procident.ADOPT_WORKER_TAG,), in_flight=None):
    body = {
        "host": host,
        **HB,
        "arch": "x86_64",
        "os": "linux",
        "gpu": False,
        "tags": list(tags),
        "host_aliases": ["any"],
    }
    if in_flight is not None:
        body["in_flight_job_ids"] = in_flight
    r = client.post("/heartbeat", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _adopt(client, **over):
    body = {
        "pid": 4242,
        "start_ticks": 1000,
        "host": "w1",
        "project": "project-a",
        "cmd": ["python", "train.py"],
        "cwd": "/tmp",
        **over,
    }
    return client.post("/adopt", json=body)


def _events(logs_dir, kind):
    path = logs_dir / "events.jsonl"
    rows = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
    return [r for r in rows if r.get("event") == kind]


def test_adopt_creates_a_running_job_bound_to_the_worker(client_logs):
    client, logs = client_logs
    _heartbeat(client)
    r = _adopt(client, gpu=True)
    assert r.status_code == 200, r.text
    job = r.json()
    assert (job["state"], job["worker"], job["host_pin"]) == ("running", "w1", "w1")
    assert (job["adopt_pid"], job["adopt_start_ticks"]) == (4242, 1000)
    assert job["preemptible"] is False and job["max_retries"] == 0
    assert job["requires"]["gpu"] is True and job["exit_code"] is None
    ev = _events(logs, "job_adopted")
    assert [(e["job_id"], e["payload"]["pid"], e["payload"]["worker"]) for e in ev] == [
        (job["id"], 4242, "w1")
    ]
    # the heartbeat response is the delivery channel — to THIS host only
    assert [j["id"] for j in _heartbeat(client)["adopted"]] == [job["id"]]
    assert _heartbeat(client, host="w2")["adopted"] == []


def test_adopt_refuses_a_worker_that_cannot_watch(client):
    assert _adopt(client).status_code == 404  # nobody registered as w1
    _heartbeat(client, tags=())  # a worker that predates adoption
    r = _adopt(client)
    assert r.status_code == 409 and "jobd-adopt" in r.json()["detail"]
    assert client.get("/jobs").json() == [], "a refused adoption must not leave a row"
    _heartbeat(client)  # positive control: same worker, now advertising support
    assert _adopt(client).status_code == 200
    dup = _adopt(client)
    assert dup.status_code == 409 and "already adopted" in dup.json()["detail"]


def test_adopt_rejects_launch_knobs_instead_of_ignoring_them(client):
    _heartbeat(client)
    r = _adopt(client, max_retries=3)
    assert r.status_code == 422 and "max_retries" in r.text
    assert _adopt(client).status_code == 200


def test_an_adopted_row_is_never_dispatched_even_if_something_requeues_it(client):
    """jobd must never RUN an adopted job's recorded command. /adopt never makes a
    QUEUED row, so force one: the claim path has to decline it on its own."""
    _heartbeat(client)
    adopted_id = _adopt(client).json()["id"]
    from jobd.db import Job

    with client.app.state.SessionLocal() as s:
        row = s.get(Job, adopted_id)
        row.state, row.worker, row.host_pin = "queued", None, "any"
        s.commit()
    assert client.post("/next-job", json={"host": "w1", **HB}).json() is None
    # positive control: an ordinary queued job IS claimed by the same poll
    normal = client.post(
        "/submit", json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"}
    ).json()["id"]
    claimed = client.post("/next-job", json={"host": "w1", **HB}).json()
    assert claimed is not None and claimed["id"] == normal


def test_an_adopted_job_holds_its_slot_against_an_already_parked_poll(client):
    """Found by the live run: a worker's long-poll parked BEFORE the adoption was
    answered with a job, because only the worker enforced slots and it had not
    heard of the adoption yet. The broker has, so the claim declines."""
    _heartbeat(client)  # single slot
    jid = _adopt(client).json()["id"]
    normal = client.post(
        "/submit", json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"}
    ).json()["id"]
    assert client.post("/next-job", json={"host": "w1", **HB}).json() is None
    # another host is not affected by w1's adoption...
    _heartbeat(client, host="w2")
    # ...and neither is w1 once the adopted process is gone (positive control)
    client.post(
        f"/jobs/{jid}/complete",
        json={"final_state": "orphaned", "termination_reason": "adopted_exit_unobserved"},
        headers={"X-Jobd-Worker": "w1"},
    )
    assert client.post("/next-job", json={"host": "w1", **HB}).json()["id"] == normal


def test_a_multislot_worker_keeps_claiming_beside_an_adopted_job(client):
    body = {
        "host": "w1",
        **HB,
        "tags": [procident.ADOPT_WORKER_TAG],
        "host_aliases": ["any"],
        "max_concurrent": 2,
    }
    assert client.post("/heartbeat", json=body).status_code == 200
    _adopt(client)
    ids = [
        client.post(
            "/submit", json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a"}
        ).json()["id"]
        for _ in range(2)
    ]
    assert client.post("/next-job", json={"host": "w1", **HB}).json()["id"] == ids[0]
    # 1 adopted + 1 claimed = 2 of 2 slots: the second waits
    assert client.post("/next-job", json={"host": "w1", **HB}).json() is None


def test_exit_of_an_adopted_process_is_orphaned_not_completed(client):
    _heartbeat(client)
    jid = _adopt(client).json()["id"]
    strict = client.post(
        "/submit",
        json={"cmd": ["true"], "cwd": "/tmp", "project": "project-a", "depends_on": [jid]},
    ).json()["id"]
    any_exit = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/tmp",
            "project": "project-a",
            "depends_on": [jid],
            "depends_on_any_exit": True,
        },
    ).json()["id"]
    assert client.post("/next-job", json={"host": "w1", **HB}).json() is None  # both blocked
    r = client.post(
        f"/jobs/{jid}/complete",
        json={
            "exit_code": None,
            "final_state": "orphaned",
            "termination_reason": "adopted_exit_unobserved",
        },
        headers={"X-Jobd-Worker": "w1"},
    )
    done = r.json()
    assert (done["state"], done["exit_code"], done["termination_reason"]) == (
        "orphaned",
        None,
        "adopted_exit_unobserved",
    )
    # success was not observed, so a child that needs success does not run...
    assert client.get(f"/jobs/{strict}").json()["state"] == "cancelled"
    # ...and "after it ends, however it ended" does.
    assert client.post("/next-job", json={"host": "w1", **HB}).json()["id"] == any_exit
    assert _heartbeat(client)["adopted"] == []


def test_cancel_reaches_the_worker_as_a_signal_and_preempt_is_refused(client):
    _heartbeat(client)
    jid = _adopt(client).json()["id"]
    assert client.get(f"/jobs/{jid}/signal").json() == {"signal": None}
    assert client.post(f"/jobs/{jid}/preempt").status_code == 409
    assert client.post(f"/jobs/{jid}/cancel").json()["state"] == "running"
    assert client.get(f"/jobs/{jid}/signal").json() == {"signal": "cancel"}


def test_a_database_from_before_adoption_upgrades_in_place(tmp_path):
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.orm import sessionmaker
    from starlette.testclient import TestClient

    from jobd.app import build_app
    from jobd.broker.jobinfo import _to_info
    from jobd.db import Job, init_db, migrate

    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = create_engine(url)
    init_db(engine)
    with sessionmaker(engine)() as s:
        s.add(
            Job(
                project="project-a",
                priority=50,
                state="queued",
                cmd_json='["true"]',
                cwd="/tmp",
                submitted_at=datetime(2026, 1, 1),
            )
        )
        s.commit()
    with engine.begin() as conn:  # back to the pre-upgrade table shape
        for col in ("adopt_pid", "adopt_start_ticks"):
            conn.execute(text(f"ALTER TABLE jobs DROP COLUMN {col}"))
    assert "adopt_pid" not in {c["name"] for c in inspect(engine).get_columns("jobs")}
    migrate(engine)
    migrate(engine)  # idempotent
    assert {"adopt_pid", "adopt_start_ticks"} <= {
        c["name"] for c in inspect(engine).get_columns("jobs")
    }
    with sessionmaker(engine)() as s:
        info = _to_info(s.get(Job, 1))
    assert (info.adopt_pid, info.adopt_start_ticks) == (None, None)
    engine.dispose()
    # The old row is an ordinary job and must still dispatch: the claim path's
    # `adopt_pid IS NULL` filter reads the back-filled NULL as "not adopted".
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "projects.yaml").write_text("projects:\n  project-a: { priority: 50 }\n")
    (cfg / "profiles.yaml").write_text("profiles: {}\n")
    (cfg / "classifier.yaml").write_text("rules: []\n")
    client = TestClient(
        build_app(
            db_url=url,
            projects_path=cfg / "projects.yaml",
            profiles_path=cfg / "profiles.yaml",
            classifier_path=cfg / "classifier.yaml",
            logs_path=tmp_path / "logs",
        )
    )
    _heartbeat(client)
    assert client.post("/next-job", json={"host": "w1", **HB}).json()["id"] == 1


# ----------------------------------------------------------------- worker glue


class _FakeBroker:
    """Stands in for the httpx client the worker threads share."""

    def __init__(self):
        self.completed: list[tuple[str, dict]] = []

    def get(self, path, timeout=None):
        import httpx

        return httpx.Response(200, json={"signal": None})

    def post(self, path, json=None, timeout=None):
        import httpx

        self.completed.append((path, json))
        return httpx.Response(200, json={})


@pytest.fixture
def clean_worker_state(monkeypatch):
    def reset():
        with job_worker._in_flight_lock:
            job_worker._in_flight.clear()
            job_worker._adopt_done.clear()
        with job_worker._in_flight_pids_lock:
            job_worker._in_flight_pids.clear()
            job_worker._adopted_roots.clear()

    reset()
    monkeypatch.setattr(job_worker, "SIGNAL_POLL_INTERVAL_S", 0.05)
    yield
    reset()


def test_adopted_vram_is_owned_not_unregistered_and_not_reserved_twice(
    sleeper, clean_worker_state, monkeypatch
):
    """The crux. The process holds 4 GB and the job declares 6. Before adoption
    those 4 GB are foreign load. After: they are the job's, only the 2 GB it does
    not yet hold are reserved on top, and none of it is still 'unregistered'."""
    monkeypatch.setattr(job_worker, "nvidia_processes", lambda: [(sleeper.pid, 4096)])
    monkeypatch.setattr(job_worker, "nvidia_free_vram_gb", lambda: 20.0)
    tracked: set[int] = set()
    before = job_worker.resource_snapshot(tracked)
    assert (before["unregistered_vram_gb"], before["free_vram_gb"]) == (4.0, 20.0)
    assert procident.ADOPT_WORKER_TAG in before["tags"]

    broker, stop = _FakeBroker(), threading.Event()
    job = {
        "id": 77,
        "adopt_pid": sleeper.pid,
        "adopt_start_ticks": _ticks(sleeper.pid),
        "vram_gb": 6.0,
    }
    assert job_worker._start_adopted_watchers(broker, [job], tracked, stop) == [77]
    assert _until(lambda: sleeper.pid in job_worker._tracked_pids_snapshot(tracked))
    during = job_worker.resource_snapshot(tracked)
    assert (during["unregistered_vram_gb"], during["free_vram_gb"]) == (0.0, 18.0)
    assert during["in_flight_job_ids"] == [77] and during["running"] == 1
    assert during["in_flight_pids"] == {"77": [sleeper.pid]}
    # the broker re-lists it on every heartbeat; that must not start a second watcher
    assert job_worker._start_adopted_watchers(broker, [job], tracked, stop) == []

    sleeper.kill()
    assert _until(lambda: broker.completed), "no terminal report after the process died"
    assert broker.completed == [
        (
            "/jobs/77/complete",
            {
                "exit_code": None,
                "final_state": "orphaned",
                "termination_reason": "adopted_exit_unobserved",
            },
        )
    ]
    assert _until(lambda: job_worker._in_flight_ids() == [])
    assert job_worker._tracked_pids_snapshot(tracked) == set()
    # a heartbeat response computed before that report landed still lists it
    assert job_worker._start_adopted_watchers(broker, [job], tracked, stop) == []


def test_a_forked_child_of_the_adopted_process_is_owned_too(clean_worker_state, monkeypatch):
    parent = subprocess.Popen(["bash", "-c", "sleep 30 & wait"])
    try:
        assert _until(lambda: psutil.Process(parent.pid).children())
        child = psutil.Process(parent.pid).children()[0].pid
        monkeypatch.setattr(job_worker, "nvidia_processes", lambda: [(child, 2048)])
        monkeypatch.setattr(job_worker, "nvidia_free_vram_gb", lambda: 20.0)
        tracked: set[int] = set()
        assert job_worker.resource_snapshot(tracked)["unregistered_vram_gb"] == 2.0
        job = {"id": 78, "adopt_pid": parent.pid, "adopt_start_ticks": _ticks(parent.pid)}
        stop = threading.Event()
        job_worker._start_adopted_watchers(_FakeBroker(), [job], tracked, stop)
        assert _until(lambda: 78 in job_worker._adopted_roots)
        assert job_worker.resource_snapshot(tracked)["unregistered_vram_gb"] == 0.0
        stop.set()
        assert _until(lambda: job_worker._in_flight_ids() == [])
    finally:
        subprocess.run(["pkill", "-KILL", "-P", str(parent.pid)], check=False)
        parent.kill()
        parent.wait(timeout=10)


def test_worker_reports_a_refused_adoption_as_failed_with_the_reason(sleeper, clean_worker_state):
    broker, tracked = _FakeBroker(), set()
    job = {"id": 79, "adopt_pid": sleeper.pid, "adopt_start_ticks": _ticks(sleeper.pid) + 1}
    job_worker._start_adopted_watchers(broker, [job], tracked, threading.Event())
    assert _until(lambda: broker.completed)
    assert broker.completed[0][1] == {
        "exit_code": None,
        "final_state": "failed",
        "termination_reason": "adopt_pid_mismatch",
    }
    assert sleeper.poll() is None
    assert _until(lambda: job_worker._in_flight_ids() == [])


# ------------------------------------------------------------------------- CLI


def test_cli_adopt_sends_the_real_identity_of_the_pid(sleeper, client, monkeypatch):
    import job_cli.cli as cli_mod

    class _Via:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def adopt(self, payload):
            r = client.post("/adopt", json=payload)
            assert r.status_code == 200, r.text
            return r.json()

    monkeypatch.setattr(cli_mod, "_client", lambda: _Via())
    monkeypatch.setenv("JOBD_WORKER_HOST", "w1")
    _heartbeat(client)
    r = CliRunner().invoke(cli_mod.app, ["adopt", "--pid", str(sleeper.pid), "--project", "p"])
    assert r.exit_code == 0, r.output
    job = json.loads(r.stdout)
    assert job["state"] == "running" and job["worker"] == "w1"
    assert (job["adopt_pid"], job["adopt_start_ticks"]) == (sleeper.pid, _ticks(sleeper.pid))
    assert job["cmd"] == ["sleep", "30"] and job["cwd"] == os.getcwd()

    gone = subprocess.Popen(["true"])
    gone.wait(timeout=10)
    r = CliRunner().invoke(cli_mod.app, ["adopt", "--pid", str(gone.pid), "--project", "p"])
    assert r.exit_code == 1 and "no such process" in r.output
