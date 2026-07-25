"""The broker must keep accepting the payload an OLDER worker sends.

jobd runs mixed versions BY DESIGN, not by accident: install-worker.sh pins a
new worker to the broker's version so it is never ahead, update-worker.sh
DEFERS an upgrade while the worker is busy (a laptop sat on 0.5.25 against a
0.5.28 broker for days), and the `version_drift` event exists precisely because
a prolonged mismatch is expected. So "older worker, newer broker" is a supported
state, and the wire contract has to survive it.

Nothing enforced that. `NextJobQuery` and `WorkerHeartbeat` declare required
fields with no defaults, so ADDING one silently locks out every worker that
predates it — and the worker cannot tell, because its poll loop treats any
non-200 from /next-job as "no job" (audit 2026-07-25 V-1; the logging half of
that fix lives in job_worker.py).

These payloads are FROZEN. Do not add keys to make a new field pass — that
defeats the entire point. If a genuinely new field is needed, give it a default
so old workers keep working, and only then decide whether the golden payload
should learn about it.
"""

from __future__ import annotations

import pytest

# The minimum a worker has ever had to send to claim work. Frozen deliberately.
_OLDEST_NEXT_JOB = {
    "host": "old-worker",
    "free_vram_gb": 0.0,
    "unregistered_vram_gb": 0.0,
    "free_ram_gb": 8.0,
    "idle_cpus": 2,
}

# The heartbeat is what keeps a worker VISIBLE. If it starts 422ing, the worker
# vanishes from `job workers` entirely; if only /next-job 422s, the worker looks
# perfectly healthy while never claiming anything. Both shapes are pinned.
_OLDEST_HEARTBEAT = {
    "host": "old-worker",
    "free_vram_gb": 0.0,
    "unregistered_vram_gb": 0.0,
    "free_ram_gb": 8.0,
    "idle_cpus": 2,
    "arch": "x86_64",
    "os": "linux",
    "gpu": False,
    "tags": [],
}

_FAILURE_HINT = (
    "A required field was added to a worker-facing request model. Every worker "
    "older than that change now gets 422 and can no longer {what} — silently, "
    "because the poll loop cannot distinguish a refusal from an idle queue. "
    "Give the new field a default instead of making it required."
)


def test_an_older_workers_claim_is_still_accepted(client):
    r = client.post("/next-job", json=dict(_OLDEST_NEXT_JOB))
    assert r.status_code == 200, f"{_FAILURE_HINT.format(what='claim jobs')}\n{r.text}"


def test_an_older_workers_heartbeat_is_still_accepted(client):
    r = client.post("/heartbeat", json=dict(_OLDEST_HEARTBEAT))
    assert r.status_code == 200, f"{_FAILURE_HINT.format(what='register')}\n{r.text}"


def test_an_older_worker_can_actually_claim_a_queued_job(client):
    """End-to-end, not just schema acceptance: register and claim with the
    frozen payloads and confirm a real job comes back."""
    submitted = client.post("/submit", json={"project": "p", "cmd": ["echo", "hi"], "cwd": "/tmp"})
    assert submitted.status_code == 200, submitted.text

    client.post("/heartbeat", json=dict(_OLDEST_HEARTBEAT))
    claim = client.post("/next-job", json=dict(_OLDEST_NEXT_JOB))

    assert claim.status_code == 200, claim.text
    assert claim.json() is not None, (
        "an older worker registered and polled but was handed nothing — the "
        "contract accepts its payload yet the job never reaches it"
    )
    assert claim.json()["id"] == submitted.json()["id"]


@pytest.mark.parametrize("dropped", sorted(_OLDEST_NEXT_JOB))
def test_the_frozen_payload_is_minimal(client, dropped):
    """Guards the guard: every key in the frozen payload must be load-bearing.

    If dropping a key still yields 200, that key is optional and does not belong
    in a compatibility floor — a bloated golden payload would keep passing after
    a required field was added, which is exactly the failure it exists to catch.
    """
    payload = {k: v for k, v in _OLDEST_NEXT_JOB.items() if k != dropped}
    r = client.post("/next-job", json=payload)
    assert r.status_code == 422, (
        f"{dropped!r} is optional, so it is dead weight in the compatibility "
        "floor — drop it from _OLDEST_NEXT_JOB."
    )
