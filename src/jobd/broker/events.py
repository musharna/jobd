"""events.jsonl emission and `since=` filter parsing."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import HTTPException

from jobd import events as _events
from jobd.broker.constants import _SINCE_RELATIVE_RE
from jobd.metrics import EVENTS_TOTAL
from jobd.models import KNOWN_EVENTS

log = logging.getLogger("jobd")


def _parse_since(s: str) -> datetime:
    """Parse '24h'/'7d'/'2w' relative or ISO-8601 absolute → cutoff datetime (UTC).

    Events with ts >= cutoff are included. Mirrors the positive-only guard
    of job_cli.cli._parse_since.
    """
    s = s.strip()
    m = _SINCE_RELATIVE_RE.match(s)
    if m:
        n = int(m.group(1))
        if n <= 0:
            raise HTTPException(status_code=400, detail=f"invalid since={s!r}: must be positive")
        unit = m.group(2)
        delta = {
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
            "w": timedelta(weeks=n),
        }[unit]
        return datetime.now(UTC) - delta
    # Python ≥3.11 fromisoformat accepts trailing Z, but be defensive on older
    # variants by normalising it explicitly.
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"invalid since={s!r}: {e}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _emit_event(
    logs_dir: Path,
    event: str,
    *,
    source: str,
    job_id: int | None = None,
    project: str | None = None,
    **payload,
) -> None:
    """Append a single schema-v2 JSON line to logs_dir/events.jsonl.

    Schema: {ts, source, event, job_id, project, payload}.
    Best-effort: errors are logged at WARNING and swallowed so observability
    never breaks broker liveness.
    """
    # M-3: mirror every event to a Prometheus Counter for rate-based alerting.
    # Guarded like the jsonl append below — observability never breaks liveness.
    # The label is allowlisted (audit 2026-07-15 Sec-A): `event` arrives
    # free-form from POST /events (hooks name their own events), and every
    # distinct label value is a PERMANENT time series in the in-process
    # registry — unbounded names would let any token holder (or a buggy hook
    # loop) grow the broker's memory and the /metrics scrape body without
    # limit. Unknown names all share one bucket; events.jsonl keeps the real
    # name at full fidelity.
    try:
        label = event if event in KNOWN_EVENTS else "other"
        EVENTS_TOTAL.labels(event=label, source=source).inc()
    except Exception as e:  # pragma: no cover - defensive
        log.warning("event metric inc failed (%s): %s", event, e)

    row = {
        "ts": datetime.now(UTC).isoformat(),
        "source": source,
        "event": event,
        "job_id": job_id,
        "project": project,
        "payload": payload,
    }
    try:
        _events.append_event(logs_dir, row)
    except Exception as e:
        log.warning("event emit failed (%s): %s", event, e)
