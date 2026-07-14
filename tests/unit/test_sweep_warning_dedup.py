"""A job that stays blocked must warn ONCE, not once per sweep.

The sweeper decides whether to re-emit a warning by comparing the newly-computed text
against the one stored on the job (`if j.warning != new_w`). That guard is only as good
as the stability of the string: the blocked-warning used to read

    queue-age 47m: blocked by non-preemptible job 2846 on laptop; ...

and the `47m` ticked on its own, so the comparison was true on every sweep forever. One
blocked job re-emitted a `sweep_warning` event *and* rewrote its DB row every minute it
stayed blocked — job 2846 alone produced 246 events. The unmatcheable warning shares the
exact same code path, carries no clock, and deduped correctly: one event, not 561. That
contrast is what identified the bug.

Same class as the dispatch_skip storm (a dedup key poisoned by a value that changes on
its own), so it gets the same treatment: a test that fails if the clock comes back.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import update

from jobd.broker.constants import _BLOCKED_WARNING_PREFIX
from jobd.db import Job


def _events(logs_dir: Path, name: str) -> list[dict]:
    f = logs_dir / "events.jsonl"
    if not f.exists():
        return []
    rows = [json.loads(line) for line in f.read_text().strip().splitlines() if line.strip()]
    return [r for r in rows if r.get("event") == name]


def _worker(host="laptop"):
    return {
        "host": host,
        "host_aliases": [host, "any"],
        "free_vram_gb": 0,
        "unregistered_vram_gb": 0,
        "free_ram_gb": 32,
        "idle_cpus": 8,
        "arch": "x86_64",
        "os": "linux",
        "gpu": False,
        "tags": [],
        "mount_roots": ["/tmp"],
    }


def _submit(client, **kw):
    body = {"cmd": ["true"], "cwd": "/tmp", "project": "project-a"}
    body.update(kw)
    r = client.post("/submit", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_blocked_job_warns_once_not_once_per_sweep(client, logs_dir):
    """The regression guard. Many sweeps, one warning."""
    client.post("/heartbeat", json=_worker())

    # A non-preemptible job occupies the only worker...
    blocker = _submit(client)
    assert client.post("/next-job", json=_worker()).json()["id"] == blocker
    client.post(f"/jobs/{blocker}/started")

    # ...and a second job queues behind it, old enough to trip the 5-minute threshold.
    queued = _submit(client)
    with client.app.state.engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == queued)
            .values(submitted_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=45))
        )

    # Sweep repeatedly, AGEING THE JOB BETWEEN SWEEPS.
    #
    # The ageing is load-bearing, and getting this wrong is how the test would quietly
    # prove nothing: the old bug's trigger was the *rendered* queue age changing, so with
    # a frozen clock the buggy code renders the same minute count every sweep and dedupes
    # correctly. A test that sweeps 5× without advancing time passes against the bug it
    # is named for. (I wrote that version first; the mutation run caught it.)
    #
    # In production the clock advances on its own, so a job blocked for four hours
    # re-emitted ~240 times. Simulate that by pushing submitted_at further back each pass.
    for minutes in (45, 46, 47, 48, 49):
        with client.app.state.engine.begin() as conn:
            conn.execute(
                update(Job)
                .where(Job.id == queued)
                .values(
                    submitted_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=minutes)
                )
            )
        client.app.state.sweep_once()

    rows = _events(logs_dir, "sweep_warning")
    blocked = [
        r for r in rows if str(r["payload"]["warning_text"]).startswith(_BLOCKED_WARNING_PREFIX)
    ]
    assert len(blocked) == 1, (
        f"expected ONE blocked sweep_warning across 5 sweeps, got {len(blocked)}. The "
        "warning text is the dedup key — if it carries a value that changes on its own "
        "(a queue age, a timestamp), the job re-emits an event and rewrites its row on "
        f"every sweep, forever. Texts seen: {[r['payload']['warning_text'] for r in blocked]}"
    )
    assert blocked[0]["job_id"] == queued


def test_blocked_warning_carries_no_self_ticking_value(client):
    """The stored warning must be stable across sweeps — assert it directly.

    Belt and braces for the test above: even if event emission were deduped some other
    way, a warning string that changes every minute still rewrites the DB row every
    minute and still misleads anyone who compares it.
    """
    client.post("/heartbeat", json=_worker())
    blocker = _submit(client)
    client.post("/next-job", json=_worker())
    client.post(f"/jobs/{blocker}/started")
    queued = _submit(client)
    with client.app.state.engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == queued)
            .values(submitted_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=45))
        )

    client.app.state.sweep_once()
    first = client.get(f"/jobs/{queued}").json()["warning"]
    assert first and first.startswith(_BLOCKED_WARNING_PREFIX), first

    # Age the job further and sweep again: the warning must be IDENTICAL. If it moves,
    # it is carrying a clock and the dedup guard is dead.
    with client.app.state.engine.begin() as conn:
        conn.execute(
            update(Job)
            .where(Job.id == queued)
            .values(submitted_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=180))
        )
    client.app.state.sweep_once()
    second = client.get(f"/jobs/{queued}").json()["warning"]

    assert second == first, (
        "the blocked warning changed when only the job's AGE changed:\n"
        f"  before: {first!r}\n  after:  {second!r}\n"
        "It is carrying a self-ticking value, which makes the dedup comparison "
        "always-true — every sweep re-emits an event and rewrites the row."
    )
