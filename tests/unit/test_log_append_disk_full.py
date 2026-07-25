"""A full disk must not turn every log chunk into a 500.

Audit 2026-07-25 M-1. `POST /jobs/{id}/log` wrote the chunk with no guard, so
ENOSPC propagated: FastAPI returned 500, the worker's `stream_output` swallowed
it and kept going — the job survived but its output vanished silently, while the
broker wrote a stack trace per chunk to the same full disk.

`_emit_event` already holds the right principle one module over
("observability never breaks broker liveness"). A log append is the same class
of write; this pins that it gets the same treatment, and that the loss is
REPORTED rather than silent.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _submit_and_claim(client) -> int:
    r = client.post("/submit", json={"project": "p", "cmd": ["echo", "hi"], "cwd": "/tmp"})
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    caps = {
        "host": "w1",
        "free_vram_gb": 0.0,
        "unregistered_vram_gb": 0.0,
        "free_ram_gb": 16.0,
        "idle_cpus": 4,
    }
    client.post("/heartbeat", json={**caps, "arch": "x86_64", "os": "linux", "gpu": False})
    claim = client.post("/next-job", json=caps)
    assert claim.status_code == 200, claim.text
    return job_id


@pytest.fixture
def full_disk(monkeypatch):
    """Every filesystem write raises ENOSPC, as a full disk would. Patched on
    Path itself so the job-log append hits it exactly as production would;
    monkeypatch restores it at teardown."""

    def _enospc(self, *a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "open", _enospc)


def test_log_append_on_a_full_disk_does_not_500(client, full_disk):
    job_id = _submit_and_claim(client)

    r = client.post(f"/jobs/{job_id}/log", content=b"some output\n")

    assert r.status_code == 200, (
        f"a full disk produced {r.status_code} — the worker sees an error it cannot act on "
        "and retries, while the broker writes a stack trace per chunk to the same full disk"
    )
    body = r.json()
    assert body["bytes"] == 0
    # The loss must be REPORTED, not silent — that is the whole point.
    assert body["dropped"] == len(b"some output\n")
    assert body["reason"] == "OSError"


def test_a_healthy_disk_still_records_the_bytes(client):
    """The control: the guard must not swallow successful writes."""
    job_id = _submit_and_claim(client)

    r = client.post(f"/jobs/{job_id}/log", content=b"hello\n")

    assert r.status_code == 200
    assert r.json() == {"bytes": 6}
    assert "hello" in client.get(f"/jobs/{job_id}/output").json()["tail"]
