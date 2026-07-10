"""H-4 (audit 2026-07-10): property-based fuzzing of the broker state machine.

The 2026-07-10 audit's systemic finding: the two code HIGHs (H-1 submit TOCTOU,
H-2 worker terminal strand) shared a root — no real-execution coverage caught
them, because every existing test is a hand-picked scenario. This harness lets
hypothesis generate arbitrary interleavings of submit / claim / start / complete
/ cancel / worker-death-reclaim / sweep against the REAL broker (in-process
FastAPI + real SQLite + the real sweeper) and asserts the invariants the CAS
discipline is supposed to guarantee:

  1. No terminal clobber — once a job reaches a terminal state it never leaves it
     or changes to a different terminal (the read-check-write clobber class the
     CAS guards close: a late /complete over a sweeper ORPHANED, etc.).
  2. Terminally cancelled at most once — no job emits >1 terminal `job_cancelled`
     (by=user|cascade); rules out double-cancel / cascade-vs-sweeper duplicates.
  3. A reclaim honors a pending user cancel (audit 2026-07-05 A2) — a job whose
     cancel was acknowledged while non-terminal, when its worker dies and the
     sweeper reclaims it, must land in CANCELLED, never silently requeue and
     re-run.

Sequential-but-arbitrary ordering (hypothesis stateful) exposes the read-decide
-write windows without needing OS threads: a cancel between claim and complete,
a stale worker completing a reclaimed job, a scheduling_timeout tripping mid
-flight, etc. Bounded by default (JOBD_H4_EXAMPLES / JOBD_H4_STEPS env overrides
for a deep nightly run).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, invariant, rule
from sqlalchemy import select, update

from jobd.app import build_app
from jobd.db import Job, Worker
from jobd.models import TERMINAL_STATES, JobState

_WORKERS = ["w1", "w2"]
_TERMINAL = {s.value for s in TERMINAL_STATES}
_VALID = {s.value for s in JobState}


def _worker_body(host: str) -> dict:
    return {
        "host": host,
        "free_vram_gb": 0,
        "unregistered_vram_gb": 0,
        "free_ram_gb": 64,
        "idle_cpus": 16,
        "arch": "x86_64",
        "os": "linux",
        "gpu": False,
        "tags": [],
        "host_aliases": [],
    }


class BrokerStateMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        # NB determinism: worker death is driven ONLY by the explicit backdate in
        # kill_worker (past the fixed DEAD_WORKER_SECONDS reclaim constant). No
        # global env is mutated — an example runs in well under the 60s stale
        # threshold, so no incidental time transition fires. (Mutating
        # os.environ here would leak into sibling tests in the same process.)
        self._dir = Path(tempfile.mkdtemp(prefix="jobd-h4-"))
        (self._dir / "projects.yaml").write_text("projects:\n  _default: { priority: 40 }\n")
        (self._dir / "profiles.yaml").write_text("profiles: {}\n")
        (self._dir / "classifier.yaml").write_text("rules: []\n")
        app = build_app(
            db_url=f"sqlite:///{self._dir}/jobd.db",
            projects_path=self._dir / "projects.yaml",
            profiles_path=self._dir / "profiles.yaml",
            classifier_path=self._dir / "classifier.yaml",
            logs_path=self._dir / "logs",
        )
        self.client = TestClient(app)
        self.engine = app.state.engine
        self._sweep = app.state.sweep_once
        for w in _WORKERS:
            self.client.post("/heartbeat", json=_worker_body(w))
        self.owner: dict[int, str] = {}
        self.cancel_ack: set[int] = set()
        self.first_terminal: dict[int, str] = {}
        self.all_ids: set[int] = set()

    jobs = Bundle("jobs")

    # --- helpers -----------------------------------------------------------
    def _state(self, jid: int) -> str:
        with self.engine.begin() as conn:
            row = conn.execute(select(Job.state).where(Job.id == jid)).first()
        return row[0] if row else "MISSING"

    def _signal(self, jid: int) -> str | None:
        with self.engine.begin() as conn:
            row = conn.execute(select(Job.signal).where(Job.id == jid)).first()
        return row[0] if row else None

    def _events(self) -> list[dict]:
        p = self._dir / "logs" / "events.jsonl"
        if not p.exists():
            return []
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]

    def _rearm_worker(self, worker: str) -> None:
        self.client.post("/heartbeat", json=_worker_body(worker))

    # --- rules -------------------------------------------------------------
    @rule(target=jobs, with_timeout=st.booleans())
    def submit(self, with_timeout: bool):
        body = {"cmd": ["true"], "cwd": "/tmp", "project": "project-a"}
        if with_timeout:
            body["scheduling_timeout_s"] = 3600  # long; tripped explicitly below
        jid = self.client.post("/submit", json=body).json()["id"]
        self.all_ids.add(jid)
        return jid

    @rule(worker=st.sampled_from(_WORKERS))
    def claim(self, worker: str):
        self._rearm_worker(worker)
        r = self.client.post("/next-job", json=_worker_body(worker))
        if r.status_code == 200 and r.json():
            self.owner[r.json()["id"]] = worker

    @rule(job=jobs)
    def start(self, job: int):
        w = self.owner.get(job)
        if w is None:
            return
        self.client.post(f"/jobs/{job}/started", headers={"X-Jobd-Worker": w})

    @rule(job=jobs, fs=st.sampled_from(["completed", "failed", "preempted"]))
    def complete(self, job: int, fs: str):
        w = self.owner.get(job)
        if w is None:  # only the owning worker completes a claimed job
            return
        self.client.post(
            f"/jobs/{job}/complete",
            json={"exit_code": 0 if fs == "completed" else 1, "final_state": fs},
            headers={"X-Jobd-Worker": w},
        )

    @rule(job=jobs)
    def stale_complete(self, job: int):
        """A worker that does NOT own the job posts /complete — must be refused
        (409) before the CAS, never clobbering the outcome (M2)."""
        w = self.owner.get(job)
        other = "w2" if w == "w1" else "w1"
        # only meaningful when `other` is not the owner (job.worker != other)
        with self.engine.begin() as conn:
            db_worker = conn.execute(select(Job.worker).where(Job.id == job)).scalar()
        if db_worker == other:
            return
        before = self._state(job)
        r = self.client.post(
            f"/jobs/{job}/complete",
            json={"exit_code": 0, "final_state": "completed"},
            headers={"X-Jobd-Worker": other},
        )
        assert r.status_code == 409, (job, db_worker, other, r.status_code, r.text)
        assert self._state(job) == before  # stale report changed nothing

    @rule(job=jobs)
    def cancel(self, job: int):
        before = self._state(job)
        self.client.post(f"/jobs/{job}/cancel")
        after = self._state(job)
        # ASSIGNED/RUNNING cancel only sets a signal (worker/reclaim honors it later)
        if (
            before in ("assigned", "running")
            and after in ("assigned", "running")
            and self._signal(job) == "cancel"
        ):
            self.cancel_ack.add(job)

    @rule(worker=st.sampled_from(_WORKERS))
    def kill_worker(self, worker: str):
        """Worker dies mid-flight: heartbeat goes silent → sweeper reclaims its
        in-flight jobs. A reclaimed job with a pending cancel MUST be honored."""
        pending = {
            j
            for j, w in self.owner.items()
            if w == worker and j in self.cancel_ack and self._state(j) not in _TERMINAL
        }
        with self.engine.begin() as conn:
            conn.execute(
                update(Worker)
                .where(Worker.host == worker)
                .values(last_heartbeat=datetime.now(UTC) - timedelta(hours=1))
            )
        self._sweep()
        # A2: a reclaim must not SILENTLY DROP a pending cancel and let the job
        # re-run to completion. Honoring it terminally (CANCELLED from an ASSIGNED
        # requeue, ORPHANED from a RUNNING non-idempotent reclaim) is fine, as is
        # requeuing with the cancel signal still armed. The bug would be a job
        # back on a runnable path with the signal erased.
        for j in pending:
            state, sig = self._state(j), self._signal(j)
            assert state in ("cancelled", "orphaned") or sig == "cancel", (j, state, sig)
            if state in _TERMINAL:
                self.cancel_ack.discard(j)
        # requeued (non-cancelled) jobs lose their owner
        for j, w in list(self.owner.items()):
            if w == worker and self._state(j) == "queued":
                del self.owner[j]
        self._rearm_worker(worker)

    @rule(job=jobs)
    def trip_scheduling_timeout(self, job: int):
        """Backdate a queued job's clock past its scheduling_timeout and sweep."""
        if self._state(job) != "queued":
            return
        old = datetime.now(UTC) - timedelta(hours=2)
        with self.engine.begin() as conn:
            conn.execute(
                update(Job).where(Job.id == job).values(submitted_at=old, last_enqueued_at=old)
            )
        self._sweep()

    @rule()
    def sweep(self):
        self._sweep()

    # --- invariants --------------------------------------------------------
    @invariant()
    def states_valid_and_no_terminal_clobber(self):
        # One batched query for every job state (cheap; runs after every step).
        # (1) every state is a known enum value; (2) once terminal, a job never
        # changes state again — the read-check-write clobber class the CAS closes.
        with self.engine.begin() as conn:
            rows = conn.execute(select(Job.id, Job.state)).all()
        for jid, s in rows:
            assert s in _VALID, (jid, s)
            if jid in self.first_terminal:
                assert s == self.first_terminal[jid], (jid, self.first_terminal[jid], s)
            elif s in _TERMINAL:
                self.first_terminal[jid] = s

    def teardown(self) -> None:
        # Terminally-cancelled-at-most-once: cumulative, so one final pass over
        # the event log at teardown catches any duplicate (checking every step
        # re-reads the whole jsonl → O(events·steps), the harness's slow path).
        counts: dict[int, int] = {}
        for e in self._events():
            if e.get("event") == "job_cancelled" and e.get("payload", {}).get("by") in (
                "user",
                "cascade",
            ):
                counts[e["job_id"]] = counts.get(e["job_id"], 0) + 1
        dupes = {jid: c for jid, c in counts.items() if c > 1}
        try:
            assert not dupes, f"job(s) terminally cancelled more than once: {dupes}"
        finally:
            # Always release resources — an undisposed engine per example leaks
            # SQLite fds/pool across a long run (fd exhaustion → the run dies).
            self.client.close()
            self.engine.dispose()
            shutil.rmtree(self._dir, ignore_errors=True)


BrokerStateMachine.TestCase.settings = settings(
    max_examples=int(os.environ.get("JOBD_H4_EXAMPLES", "40")),
    stateful_step_count=int(os.environ.get("JOBD_H4_STEPS", "40")),
    deadline=None,  # app build + sweeps per step are not per-step-time-bounded
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

TestBrokerStateMachine = BrokerStateMachine.TestCase
