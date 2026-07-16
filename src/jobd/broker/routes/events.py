"""Observability ingest/read: POST+GET /events.

Stage-3 split (backlog 2026-07-15): endpoint bodies are VERBATIM from
app.py's build_app — build_router unpacks BrokerDeps into the same local
names the closures always captured, so the move is byte-identical at the
body level and the whole suite passes unchanged.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from jobd import events as _events
from jobd.broker.context import BrokerDeps
from jobd.broker.events import _emit_event, _parse_since
from jobd.models import EventIngest


def build_router(deps: BrokerDeps) -> APIRouter:
    router = APIRouter()
    logs_dir = deps.logs_dir

    @router.post("/events", status_code=204)
    def ingest_event(body: EventIngest):
        """Generic audit-event ingester for non-broker sources.

        Source is allowlisted to {worker, hook, mcp} by the Pydantic Literal —
        `source="broker"` yields 422, preserving the single-writer property for
        broker-emitted events. Broker stamps `ts` server-side via _emit_event.
        """
        _emit_event(
            logs_dir,
            body.event,
            source=body.source,
            job_id=body.job_id,
            project=body.project,
            **body.payload,
        )
        return Response(status_code=204)

    @router.get("/events")
    def get_events(
        since: str | None = None,
        project: str | None = None,
        event: str | None = None,
        job_id: int | None = None,
        source: str | None = None,
        limit: int = 1000,
    ):
        """Return filtered event-stream rows from events.jsonl (newest-last).

        Filters: since (relative `Nh|Nd|Nw` or ISO-8601), project, event,
        job_id, source. limit defaults to 1000, clamped to [1, 10000].

        Rows missing the `source` field (legacy pre-schema-v2) are skipped
        silently with a per-request INFO log. Malformed JSON lines and rows
        with unparseable `ts` are skipped defensively.
        """
        limit = max(1, min(10000, int(limit)))
        cutoff = _parse_since(since) if since else None

        def _match(row: dict) -> bool:
            # Rows missing `source` are legacy (pre-schema-v2) — excluded, same
            # as before. The reverse-reader handles the ts/cutoff early-stop.
            if "source" not in row:
                return False
            if project is not None and row.get("project") != project:
                return False
            if event is not None and row.get("event") != event:
                return False
            if job_id is not None and row.get("job_id") != job_id:
                return False
            if source is not None and row.get("source") != source:  # noqa: SIM103 — parallel guard-clause filter
                return False
            return True

        return _events.read_events(logs_dir, match=_match, cutoff=cutoff, limit=limit)

    return router
