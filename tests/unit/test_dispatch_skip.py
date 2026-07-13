"""dispatch_skip event emission + matcher.explain_skip predicate-chain tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from jobd.matcher import QueuedJob, WorkerSnapshot, explain_skip
from jobd.models import JobRequires


def _all_events(logs_dir: Path, event_name: str) -> list[dict]:
    rows = (logs_dir / "events.jsonl").read_text().strip().splitlines()
    parsed = [json.loads(r) for r in rows]
    return [p for p in parsed if p.get("event") == event_name]


def _heartbeat_payload(host="laptop", *, gpu=False, free_vram_gb=0, unregistered_vram_gb=0):
    return {
        "host": host,
        "host_aliases": [host, "any"],
        "free_vram_gb": free_vram_gb,
        "unregistered_vram_gb": unregistered_vram_gb,
        "free_ram_gb": 32,
        "idle_cpus": 8,
        "arch": "x86_64",
        "os": "linux",
        "gpu": gpu,
        "tags": [],
        "mount_roots": ["/tmp", "/home"],
    }


def _fake_job(
    *,
    id: int = 1,
    priority: int = 50,
    host_pin: str = "any",
    vram_gb: float = 0,
    ram_gb: float = 1,
    cpus: int = 1,
    requires: JobRequires | None = None,
) -> QueuedJob:
    ns = SimpleNamespace(
        id=id,
        priority=priority,
        submitted_at=datetime.now(UTC),
        host_pin=host_pin,
        vram_gb=vram_gb,
        ram_gb=ram_gb,
        cpus=cpus,
        requires=requires,
    )
    return cast(QueuedJob, ns)


def _laptop_snapshot(**overrides: Any) -> WorkerSnapshot:
    base: dict[str, Any] = {
        "host": "laptop",
        "host_aliases": ["laptop", "any"],
        "free_vram_gb": 0,
        "unregistered_vram_gb": 0,
        "free_ram_gb": 32,
        "idle_cpus": 8,
        "arch": "x86_64",
        "os": "linux",
        "gpu": False,
        "tags": [],
    }
    base.update(overrides)
    return WorkerSnapshot(**base)


# ---------- Step 4.2: matcher.explain_skip ----------


@pytest.mark.parametrize(
    "job_kwargs,worker_overrides,expected_reason",
    [
        # gpu=True against a non-GPU worker → "no_gpu"
        (
            {"requires": JobRequires(gpu=True)},
            {"gpu": False},
            "no_gpu",
        ),
        # arch mismatch
        (
            {"requires": JobRequires(arch="aarch64")},
            {"arch": "x86_64"},
            "arch_mismatch",
        ),
        # os mismatch
        (
            {"requires": JobRequires(os="darwin")},
            {"os": "linux"},
            "os_mismatch",
        ),
        # missing tag
        (
            {"requires": JobRequires(needs=["cuda-24gb"])},
            {"tags": ["cuda-8gb"]},
            "tags",
        ),
        # host_pin mismatch
        (
            {"host_pin": "desktop"},
            {"host": "laptop", "host_aliases": ["laptop", "any"]},
            "host_pin",
        ),
        # vram saturation: 24 GB request, 4 GB effective free
        (
            {"vram_gb": 24, "requires": JobRequires(gpu=True)},
            {"gpu": True, "free_vram_gb": 5, "unregistered_vram_gb": 0},
            "vram",
        ),
        # ram pressure
        (
            {"ram_gb": 64},
            {"free_ram_gb": 8},
            "ram",
        ),
        # cpu pressure (idle_cpus + OVERSUBSCRIBE_CPU_ALLOWANCE=2)
        (
            {"cpus": 16},
            {"idle_cpus": 2},
            "cpus",
        ),
    ],
)
def test_explain_skip_returns_per_job_reason(job_kwargs, worker_overrides, expected_reason):
    job = _fake_job(**job_kwargs)
    w = _laptop_snapshot(**worker_overrides)
    out = explain_skip([job], w)
    assert out == [(job.id, expected_reason)]


def test_explain_skip_omits_jobs_that_fit():
    fits = _fake_job(id=1)
    misses = _fake_job(id=2, requires=JobRequires(gpu=True))
    w = _laptop_snapshot()  # gpu=False
    out = explain_skip([fits, misses], w)
    assert out == [(2, "no_gpu")]


def test_explain_skip_short_circuits_in_fits_on_worker_order():
    """When multiple predicates fail, the FIRST in fits_on_worker order wins."""
    # arch-mismatch + ram-pressure on the same job — arch should be reported first
    job = _fake_job(requires=JobRequires(arch="aarch64"), ram_gb=64)
    w = _laptop_snapshot(arch="x86_64", free_ram_gb=8)
    out = explain_skip([job], w)
    assert out == [(job.id, "arch_mismatch")]


# ---------- Step 4.3: dispatch_skip event wiring ----------


def _submit_gpu_job(client, project="project-b"):
    r = client.post(
        "/submit",
        json={
            "project": project,
            "cmd": ["true"],
            "cwd": "/tmp/foo",
            "host_pin": "any",
            "requires": {"gpu": True},
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_dispatch_skip_emits_once_until_reason_changes(client, logs_dir):
    info = _submit_gpu_job(client)
    # Heartbeat a non-GPU laptop worker so it's a known worker.
    client.post("/heartbeat", json=_heartbeat_payload(gpu=False))

    payload = _heartbeat_payload(gpu=False)
    for _ in range(3):
        r = client.post("/next-job", json=payload)
        assert r.status_code == 200
        assert r.json() is None  # no dispatch — gpu mismatch

    rows = _all_events(logs_dir, "dispatch_skip")
    assert len(rows) == 1, f"expected exactly one dispatch_skip; got {rows}"
    row = rows[0]
    assert row["source"] == "broker"
    assert row["job_id"] == info["id"]
    assert row["project"] == "project-b"
    assert row["payload"]["worker"] == "laptop"
    assert row["payload"]["reason"] == "no_gpu"


def test_dispatch_skip_emits_again_on_reason_change(client, logs_dir):
    info = _submit_gpu_job(client)

    # Poll 1: non-GPU laptop → reason=no_gpu
    client.post("/heartbeat", json=_heartbeat_payload(host="laptop", gpu=False))
    r1 = client.post("/next-job", json=_heartbeat_payload(host="laptop", gpu=False))
    assert r1.status_code == 200 and r1.json() is None

    # Poll 2: GPU desktop, but VRAM saturated → reason=vram
    client.post(
        "/heartbeat",
        json=_heartbeat_payload(host="desktop", gpu=True, free_vram_gb=2, unregistered_vram_gb=0),
    )
    r2 = client.post(
        "/next-job",
        json=_heartbeat_payload(host="desktop", gpu=True, free_vram_gb=2, unregistered_vram_gb=0),
    )
    assert r2.status_code == 200 and r2.json() is None

    rows = _all_events(logs_dir, "dispatch_skip")
    assert len(rows) == 2, f"expected two dispatch_skip rows; got {rows}"
    reasons = [r["payload"]["reason"] for r in rows]
    assert reasons == ["no_gpu", "vram"]
    workers = [r["payload"]["worker"] for r in rows]
    assert workers == ["laptop", "desktop"]
    assert all(r["job_id"] == info["id"] for r in rows)


def test_dispatch_skip_does_not_restorm_when_workers_alternate(client, logs_dir):
    """THE regression guard for the dispatch_skip event storm.

    `explain_skip` computes the reason against a specific worker, so one queued job
    legitimately has DIFFERENT reasons on different hosts. The dedup state used to be
    keyed by job_id alone, so each worker's answer overwrote the previous one and the
    "has the reason changed?" test was true on every single poll — the event fired
    every time any worker polled. In production two permanently-unplaceable jobs
    emitted 3,908 of 5,340 dispatch_skip events in one sample (~54k over 7 days), each
    an append to events.jsonl.

    Alternating polls from two workers must emit exactly TWO events — one per (job,
    worker) — no matter how many times they poll. Against the old job-id-only key this
    test sees six.
    """
    info = _submit_gpu_job(client)
    laptop = _heartbeat_payload(host="laptop", gpu=False)  # -> no_gpu
    desktop = _heartbeat_payload(  # -> vram (has a GPU, but not enough free)
        host="desktop", gpu=True, free_vram_gb=2, unregistered_vram_gb=0
    )
    client.post("/heartbeat", json=laptop)
    client.post("/heartbeat", json=desktop)

    for _ in range(3):
        for payload in (laptop, desktop):
            r = client.post("/next-job", json=payload)
            assert r.status_code == 200 and r.json() is None

    rows = _all_events(logs_dir, "dispatch_skip")
    assert len(rows) == 2, (
        f"expected exactly 2 dispatch_skip events (one per worker), got {len(rows)}. "
        "More than that means the dedup key is not worker-scoped and every poll "
        "re-emits — the event storm."
    )
    assert {(r["payload"]["worker"], r["payload"]["reason"]) for r in rows} == {
        ("laptop", "no_gpu"),
        ("desktop", "vram"),
    }
    assert all(r["job_id"] == info["id"] for r in rows)


def test_dispatch_skip_state_gc_drops_jobs_no_longer_queued(client):
    """skip_state GC: when a skipped job leaves the queue (cancel/complete),
    its dedup entry is removed so the dict can't grow without bound."""
    info = _submit_gpu_job(client)
    client.post("/heartbeat", json=_heartbeat_payload(gpu=False))
    r1 = client.post("/next-job", json=_heartbeat_payload(gpu=False))
    assert r1.status_code == 200 and r1.json() is None

    skip_state = client.app.state.shared["dispatch_skip_state"]
    # Keyed by (job_id, worker_host) — the reason is per-worker, so the key must be.
    assert skip_state.get((info["id"], "laptop")) == "no_gpu"

    cancel = client.post(f"/jobs/{info['id']}/cancel")
    assert cancel.status_code == 200, cancel.text

    # Trigger another /next-job → GC pass runs since the cancelled job is no
    # longer in the queued set.
    r2 = client.post("/next-job", json=_heartbeat_payload(gpu=False))
    assert r2.status_code == 200 and r2.json() is None
    assert (info["id"], "laptop") not in skip_state, (
        f"GC should drop dedup entry for cancelled job; got {skip_state}"
    )


def test_dispatch_skip_gc_does_not_evict_other_workers_entries(client, logs_dir):
    """GC must be scoped to the polling worker's own keys.

    The queued list is filtered per-worker (mount_roots, exclusion set) before the GC
    runs, so a job merely invisible to THIS worker is still queued for others. Evicting
    their entries would make them re-emit on their next poll — a second, subtler source
    of the same storm.
    """
    info = _submit_gpu_job(client)
    laptop = _heartbeat_payload(host="laptop", gpu=False)
    desktop = _heartbeat_payload(host="desktop", gpu=True, free_vram_gb=2, unregistered_vram_gb=0)
    client.post("/heartbeat", json=laptop)
    client.post("/heartbeat", json=desktop)

    client.post("/next-job", json=laptop)
    client.post("/next-job", json=desktop)

    skip_state = client.app.state.shared["dispatch_skip_state"]
    assert (info["id"], "laptop") in skip_state
    assert (info["id"], "desktop") in skip_state, (
        "desktop's dedup entry was evicted by laptop's GC pass — it will re-emit"
    )
    assert len(_all_events(logs_dir, "dispatch_skip")) == 2
