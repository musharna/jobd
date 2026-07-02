"""Broker internals split out of the app factory (jobd.app.build_app).

The FastAPI app factory grew into a single ~2.6k-line module. These submodules
hold the pieces that don't need the app/closure scope — pure helpers over the
DB session and models — so `app.py` can shrink to wiring and endpoints:

- ``constants``   — broker tuning knobs + terminal-state sets.
- ``context``     — shared per-broker state types.
- ``events``      — events.jsonl emission + ``since=`` parsing.
- ``state``       — job state-machine transitions, cascade, heartbeat reconcile.
- ``scheduling``  — preemption/blocker probes + worker snapshots.
- ``jobinfo``     — Job ORM row -> JobInfo (+ ETA population).
- ``projects``    — projects.yaml (de)serialization.

`jobd.app` re-imports these, so existing `jobd.app.<name>` references (incl.
the ones tests monkeypatch) keep working.
"""
