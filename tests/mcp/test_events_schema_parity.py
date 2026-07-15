"""jobd_events schema ↔ event vocabulary parity (audit 2026-07-15 Q-1).

The routes and submit fields got derive-and-fail-on-drift guards in PR#48; the
EVENT vocabulary did not, and its hand-typed schema list was stale within a day
of shipping — 7 real event types (job_resurrected among them) simply weren't
advertised, so an agent debugging a resurrected job was told the filter value
didn't exist. The schema text is now DERIVED from models.KNOWN_EVENTS; these
tests keep it that way and keep the source enum honest.
"""

from __future__ import annotations

from typing import get_args

from jobd.mcp.schemas import EVENTS_INPUT
from jobd.models import KNOWN_EVENTS, EventIngest


def test_every_known_event_is_advertised_in_the_schema():
    desc = EVENTS_INPUT["properties"]["event"]["description"]
    missing = [e for e in sorted(KNOWN_EVENTS) if e not in desc]
    assert not missing, (
        f"event types the system emits but the jobd_events schema does not "
        f"advertise: {missing} — an agent is being told these filter values "
        "don't exist. The description must be derived from models.KNOWN_EVENTS, "
        "not hand-typed."
    )


def test_schema_admits_custom_hook_event_names():
    """Hook/MCP-ingested events legitimately carry names beyond the vocabulary;
    the schema must say so rather than imply the list is closed."""
    desc = EVENTS_INPUT["properties"]["event"]["description"]
    assert "custom" in desc.lower(), (
        "the schema reads as a closed enum — hooks name their own events by design"
    )


def test_source_enum_covers_every_ingest_source_plus_broker():
    """The old enum ['broker','worker'] FORBADE filtering to hook/mcp events
    even though the broker records and filters them fine."""
    schema_sources = set(EVENTS_INPUT["properties"]["source"]["enum"])
    ingest_sources = set(get_args(EventIngest.model_fields["source"].annotation))
    expected = ingest_sources | {"broker"}
    assert schema_sources == expected, (
        f"jobd_events source enum {sorted(schema_sources)} != broker reality "
        f"{sorted(expected)} (EventIngest sources + broker-emitted rows)"
    )
