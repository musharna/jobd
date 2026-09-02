"""audit 2026-09-02 L-4: the reparented-orphan /proc sweep at job finalize is
sound only while THIS job is the sole one in flight, and it gated on a point
read of `_in_flight`. Between that read and the scan the poll loop could
register job B and Popen it; B's fresh child has ppid == this worker and is not
in A's tracked set, so the sweep SIGTERMed it. Registration already takes
`_in_flight_lock` before any Popen, so holding that lock across gate + scan
makes them atomic: B cannot register while the scan runs, and if B registered
first the gate sees two jobs and declines.

Real threads, real lock; the only stub is the /proc scan itself.
"""

import threading

import jobd.worker.job_worker as jw


def _reset_in_flight(*ids):
    with jw._in_flight_lock:
        jw._in_flight.clear()
        for i in ids:
            jw._in_flight[i] = {"vram_gb": 0.0, "ram_gb": 0.0, "cpus": 0}


def test_no_job_can_register_while_the_solo_sweep_is_scanning(monkeypatch):
    _reset_in_flight(1)
    monkeypatch.setattr(jw, "_REAPER_OK", True)
    scanning = threading.Event()
    release = threading.Event()
    swept: list[set[int]] = []

    def fake_scan(known: set[int]) -> list[int]:
        scanning.set()
        assert release.wait(5), "test harness never released the scan"
        swept.append(set(known))
        return []

    monkeypatch.setattr(jw._subreaper, "sweep_and_kill_reparented_orphans", fake_scan)

    sweeper = threading.Thread(
        target=jw._sweep_reparented_orphans_if_solo, args=(1, {111}), daemon=True
    )
    sweeper.start()
    assert scanning.wait(5), "the solo sweep did not run for a lone in-flight job"

    registered = threading.Event()

    def register_b():
        jw._register_in_flight({"id": 2, "vram_gb": 0, "ram_gb": 0, "cpus": 0})
        registered.set()

    threading.Thread(target=register_b, daemon=True).start()
    assert not registered.wait(0.3), "job B registered while the solo sweep was scanning /proc"

    release.set()
    sweeper.join(5)
    assert registered.wait(5), "registration never completed after the sweep finished"
    assert swept == [{111}]
    try:
        # Positive control: with two jobs in flight the sweep must not run at all.
        calls: list[set[int]] = []
        monkeypatch.setattr(
            jw._subreaper,
            "sweep_and_kill_reparented_orphans",
            lambda known: calls.append(known) or [],
        )
        jw._sweep_reparented_orphans_if_solo(1, {111})
        assert calls == []
    finally:
        _reset_in_flight()
