"""Server-side long-poll on /next-job (jobd #52).

The broker holds /next-job up to `wait_s`, returning instantly when a job
becomes dispatchable (woken on submit / terminal transition / requeue) or at
the deadline. `wait_s` defaults to 0 → legacy instant return, so old workers
and the rest of the suite are unaffected.
"""

from __future__ import annotations

import concurrent.futures
import time


def _next_job_payload(*, host="laptop", gpu=False, free_vram_gb=0, wait_s=0.0):
    return {
        "host": host,
        "free_vram_gb": free_vram_gb,
        "unregistered_vram_gb": 0,
        "free_ram_gb": 32,
        "idle_cpus": 8,
        "arch": "x86_64",
        "os": "linux",
        "gpu": gpu,
        "tags": [],
        "mount_roots": ["/tmp", "/home"],
        "wait_s": wait_s,
    }


def _submit_cpu_job(client, project="project-b"):
    r = client.post(
        "/submit",
        json={
            "project": project,
            "cmd": ["true"],
            "cwd": "/tmp/foo",
            "host_pin": "any",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_wait_zero_returns_immediately(client):
    """Default wait_s=0 → instant None when nothing is queued (legacy path).

    The bound is 2s, not 0.5s. What this test detects is `wait_s=0` wrongly
    engaging the long-poll, and a wrongly-engaged hold lasts
    `_LONGPOLL_RECHECK_S` (10s) or the client's POLL_TIMEOUT_S (30s) — so 2s
    still catches the defect five times over while surviving ordinary runner
    jitter. The old 0.5s bound was 20× tighter than the failure it guards
    against, and it duly failed a RELEASE on a 0.59s measurement (v0.5.34,
    2026-07-25) without detecting anything extra. The sibling assertions in
    this file already use 2.0/3.0; 0.5 was the outlier.
    """
    t0 = time.monotonic()
    r = client.post("/next-job", json=_next_job_payload())  # no wait_s key → 0
    elapsed = time.monotonic() - t0
    assert r.status_code == 200
    assert r.json() is None
    assert elapsed < 2.0, f"wait_s=0 must not block; took {elapsed:.2f}s"


def test_blocks_until_deadline_when_no_job(client):
    """wait_s>0 with an empty queue holds the request ~wait_s, then returns None."""
    t0 = time.monotonic()
    r = client.post("/next-job", json=_next_job_payload(wait_s=0.5))
    elapsed = time.monotonic() - t0
    assert r.status_code == 200
    assert r.json() is None
    assert 0.4 < elapsed < 3.0, f"expected ~0.5s hold; took {elapsed:.2f}s"


def test_returns_already_queued_job_without_waiting(client):
    """A job already in the queue is returned on the first attempt — the
    long-poll never sleeps when work is waiting."""
    info = _submit_cpu_job(client)
    t0 = time.monotonic()
    r = client.post("/next-job", json=_next_job_payload(wait_s=5))
    elapsed = time.monotonic() - t0
    assert r.status_code == 200
    assert r.json() is not None
    assert r.json()["id"] == info["id"]
    assert elapsed < 2.0, f"queued job must dispatch promptly; took {elapsed:.2f}s"


def test_wake_on_submit_during_longpoll(client):
    """The real-execution check: a worker long-polling an EMPTY queue is woken
    by a /submit that lands mid-wait and returns the new job well before the
    deadline. If the wake were broken it would return None at the 5s deadline
    (the loop does no final attempt past the deadline)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        t0 = time.monotonic()
        poll = ex.submit(lambda: client.post("/next-job", json=_next_job_payload(wait_s=5)))
        # Let the poll reach its first (empty) attempt and start waiting.
        time.sleep(0.4)
        info = _submit_cpu_job(client)
        r = poll.result(timeout=8)
        elapsed = time.monotonic() - t0

    assert r.status_code == 200
    body = r.json()
    assert body is not None, "wake failed: long-poll returned None despite a mid-wait submit"
    assert body["id"] == info["id"]
    assert elapsed < 3.0, (
        f"job submitted at ~0.4s should dispatch via wake well before the 5s "
        f"deadline; took {elapsed:.2f}s (wake likely not firing)"
    )
