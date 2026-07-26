"""`max_concurrent > 1` must actually run N jobs at once, and never N+1.

The 2026-07-25 adverse-conditions audit left this open as a LOW: the setting was
covered only by *parsing* tests (`_max_concurrent_jobs()` returns 1 by default,
the API round-trips the field) and by nothing that ran two jobs at the same
time. So the advertised behaviour — the thing the README's pueue/task-spooler
migration table points migrants at as its answer to "parallelism limits" — had
no test at all. Every worker in the author's own fleet runs `max_concurrent: 1`,
so production did not exercise it either.

How the bound actually works, because the tests below depend on it:

  - ONE poll thread runs the gate. It reads ``len(_in_flight)`` and, if that is
    below max_concurrent, claims a job and calls `_reserve_and_dispatch`.
  - `_reserve_and_dispatch` calls `_register_in_flight` **synchronously, in the
    poll thread, before it returns** — then starts a daemon thread to do the
    work. That ordering is the whole bound. Its docstring records the bug it
    replaced: registering inside the worker thread left a window between
    ``t.start()`` and the thread being scheduled in which the gate could
    re-poll against a stale count and oversubscribe.
  - Job threads only ever *unregister*, concurrently with the poller.

So the invariant is not "many threads race to reserve" — it is "one poller
reserves while N workers release, and the live count never exceeds the cap."
These tests model exactly that, with real threads.
"""

from __future__ import annotations

import threading
import time

import pytest

import jobd.worker.job_worker as jw


@pytest.fixture(autouse=True)
def _clean_in_flight():
    """Never leak reservations between tests — the registry is module state."""
    with jw._in_flight_lock:
        jw._in_flight.clear()
    yield
    with jw._in_flight_lock:
        jw._in_flight.clear()


def _job(job_id: int) -> dict:
    return {"id": job_id, "vram_gb": 0, "ram_gb": 0, "cpus": 1}


# --- the ordering that bounds concurrency ---------------------------------


class _ManualThread:
    """A `threading.Thread` stand-in whose `start()` deliberately does nothing.

    The invariant under test is an *ordering* one — the reservation must be
    recorded before `_reserve_and_dispatch` returns, not merely soon after. A
    real thread makes that untestable: `Thread.start()` frequently lets the
    worker register before the main thread looks, so an assertion on the count
    passes whether the code is correct or not. That is not hypothetical — the
    first version of this file passed with the historical bug reinstated, and
    was only caught by mutation-testing it.

    With a thread that never runs on its own, the two implementations are
    distinguishable with certainty: correct code has already registered, buggy
    code has registered nothing.
    """

    def __init__(self, target=None, args=(), daemon=None, **kw):
        self._target, self._args = target, args
        self.daemon = daemon
        self.started = False

    def start(self):
        self.started = True

    def run_now(self):
        if self._target is not None:
            self._target(*self._args)

    def join(self, timeout=None):
        return None

    def is_alive(self):
        return False


def test_reservation_is_recorded_before_dispatch_returns(monkeypatch):
    """The regression guard for the bug `_reserve_and_dispatch` was written to fix.

    Deterministic: the worker thread is prevented from running at all, so the
    count can only be 1 if the reservation happened synchronously in the
    caller. Moving `_register_in_flight` back inside the thread fails this.
    """
    monkeypatch.setattr(jw.threading, "Thread", _ManualThread)
    threads: list = []

    jw._reserve_and_dispatch(_job(1), job_threads=threads, run_in_thread=lambda job: None)

    assert threads and threads[0].started, "dispatch did not start a worker thread"
    assert jw._running_count() == 1, (
        "capacity was not reserved before dispatch returned — the poll loop's "
        "gate would read a stale count and oversubscribe"
    )
    assert jw._in_flight_ids() == [1]


def test_gate_stops_claiming_at_the_cap_even_if_no_job_has_started(monkeypatch):
    """Oversubscription guard, deterministic.

    Worker threads never run, so nothing completes and nothing can unregister.
    A correct worker reserves synchronously, so the gate sees the cap after
    exactly `cap` claims and stops. A worker that registers inside the thread
    sees 0 forever and would claim without bound — caught here by the iteration
    ceiling rather than by hanging the suite.
    """
    monkeypatch.setattr(jw.threading, "Thread", _ManualThread)
    cap = 3
    ceiling = cap * 10
    threads: list = []
    dispatched = 0

    for jid in range(ceiling):
        with jw._in_flight_lock:
            live = len(jw._in_flight)
        if live >= cap:
            break
        jw._reserve_and_dispatch(_job(jid), job_threads=threads, run_in_thread=lambda job: None)
        dispatched += 1

    assert dispatched == cap, (
        f"gate admitted {dispatched} jobs against a cap of {cap} — "
        "reservations are not visible to the poll loop in time"
    )
    assert jw._running_count() == cap


def test_the_mirrored_gate_still_matches_the_real_poll_loop():
    """These tests re-implement the poll loop's capacity check rather than
    driving the loop itself, which is wrapped in HTTP. Pin the real expression
    so the mirror cannot silently drift out of agreement with the source."""
    import inspect

    source = inspect.getsource(jw)
    assert "live >= max_concurrent" in source, (
        "the poll loop's capacity gate changed shape; the mirrored gate in "
        "these tests must be updated to match"
    )


def test_completion_frees_the_slot():
    done = threading.Event()
    threads: list = []

    def run_in_thread(job):
        jw._unregister_in_flight(int(job["id"]))
        done.set()

    jw._reserve_and_dispatch(_job(7), job_threads=threads, run_in_thread=run_in_thread)
    assert done.wait(timeout=5), "job thread never ran"
    for t in threads:
        t.join(timeout=5)

    assert jw._running_count() == 0
    assert jw._in_flight_ids() == []


# --- N at once, never N+1 --------------------------------------------------


@pytest.mark.parametrize("cap", [2, 3, 5])
def test_jobs_really_do_run_simultaneously_up_to_the_cap(cap: int):
    """The positive direction: at max_concurrent=N, N jobs are in flight AT ONCE.

    A serialising worker would keep the peak at 1 and time out on the barrier,
    so this fails loudly rather than passing vacuously.
    """
    barrier = threading.Barrier(cap, timeout=10)
    release = threading.Event()
    peak = 0
    peak_lock = threading.Lock()
    threads: list = []
    barrier_broke = threading.Event()

    def run_in_thread(job):
        nonlocal peak
        with peak_lock:
            peak = max(peak, jw._running_count())
        try:
            barrier.wait()  # only passes if `cap` jobs are alive together
        except threading.BrokenBarrierError:
            barrier_broke.set()
        release.wait(timeout=5)
        jw._unregister_in_flight(int(job["id"]))

    for i in range(cap):
        assert jw._running_count() < cap, "gate would have refused this claim"
        jw._reserve_and_dispatch(_job(i), job_threads=threads, run_in_thread=run_in_thread)

    assert jw._running_count() == cap
    release.set()
    for t in threads:
        t.join(timeout=10)

    assert not barrier_broke.is_set(), f"jobs did not overlap: only {peak} ran together"
    assert peak == cap, f"expected {cap} concurrent jobs, peak was {peak}"
    assert jw._running_count() == 0


def test_poller_never_exceeds_the_cap_while_workers_finish_concurrently():
    """The real concurrency check: one poll thread reserving, many threads
    releasing, for many rounds. Models the live arrangement rather than an
    artificial all-threads-reserve race, which the design never performs.

    Any observation above the cap is a hard failure — recorded rather than
    asserted inside the thread so the error surfaces on the main thread.
    """
    cap = 4
    total_jobs = 60
    violations: list[int] = []
    threads: list = []
    next_id = iter(range(total_jobs))

    def run_in_thread(job):
        observed = jw._running_count()
        if observed > cap:
            violations.append(observed)
        jw._unregister_in_flight(int(job["id"]))

    dispatched = 0
    deadline = time.monotonic() + 30
    while dispatched < total_jobs:
        assert time.monotonic() < deadline, (
            f"dispatch loop stalled at {dispatched}/{total_jobs} — slots never freed"
        )
        # This mirrors the poll loop's gate verbatim (job_worker.py ~1647).
        with jw._in_flight_lock:
            live = len(jw._in_flight)
        if live >= cap:
            # Yield rather than busy-spin: the real loop sleeps 1.0s here, and
            # a hot spin pins a core and starves the very threads we are
            # waiting on (it pushed the full suite past its time budget).
            time.sleep(0.001)
            continue
        if live > cap:
            violations.append(live)
        jid = next(next_id)
        jw._reserve_and_dispatch(_job(jid), job_threads=threads, run_in_thread=run_in_thread)
        dispatched += 1

    for t in threads:
        t.join(timeout=10)

    assert not violations, f"capacity exceeded {cap}: observed {sorted(set(violations))}"
    assert dispatched == total_jobs
    assert jw._running_count() == 0


# --- what the broker is told ----------------------------------------------


def test_heartbeat_running_count_tracks_real_reservations():
    """`running` is what the broker stores and shows in `job workers`; it must
    be the live in-flight count, not a constant."""
    release = threading.Event()
    threads: list = []

    def run_in_thread(job):
        release.wait(timeout=5)
        jw._unregister_in_flight(int(job["id"]))

    assert jw._running_count() == 0
    for i in range(3):
        jw._reserve_and_dispatch(_job(i), job_threads=threads, run_in_thread=run_in_thread)
        assert jw._running_count() == i + 1

    assert jw._in_flight_ids() == [0, 1, 2]

    release.set()
    for t in threads:
        t.join(timeout=5)
    assert jw._running_count() == 0


def test_allocations_total_sums_across_concurrent_jobs(monkeypatch):
    """At max_concurrent>1 the heartbeat must subtract EVERY pending claim, not
    just the newest — otherwise the broker matches against VRAM the worker has
    already promised away."""
    monkeypatch.setattr(jw, "effective_vram_request_gb_from_job", lambda j: float(j["vram_gb"]))

    for i, vram in enumerate([4.0, 6.0, 2.0]):
        jw._register_in_flight({"id": i, "vram_gb": vram, "ram_gb": 1.5, "cpus": 2})

    vram_total, ram_total, cpu_total = jw._allocations_total()
    assert vram_total == pytest.approx(12.0)
    assert ram_total == pytest.approx(4.5)
    assert cpu_total == 6


# --- the setting itself ----------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", 1), ("2", 2), ("16", 16)],
)
def test_max_concurrent_jobs_reads_the_env_var(monkeypatch, value, expected):
    monkeypatch.setenv("JOBD_WORKER_MAX_CONCURRENT_JOBS", value)
    assert jw._max_concurrent_jobs() == expected


@pytest.mark.parametrize("value", ["0", "-3", "", "  ", "banana", "2.5"])
def test_max_concurrent_jobs_never_returns_a_useless_cap(monkeypatch, value):
    """A cap below 1 would wedge the poll loop forever (`live >= max` is true at
    live=0), so garbage must fall back to a working worker, not a dead one."""
    monkeypatch.setenv("JOBD_WORKER_MAX_CONCURRENT_JOBS", value)
    assert jw._max_concurrent_jobs() >= 1
