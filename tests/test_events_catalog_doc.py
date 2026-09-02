"""Every name in KNOWN_EVENTS must be documented in docs/events.md.

audit 2026-09-02: six event names landed in the delta with no catalog anywhere
but the source constant and the changelog. An operator writing an alert has to
know what `reclaim_suppressed` or `cwd_identity_applied` means; the MCP
`jobd_events` enum derives from KNOWN_EVENTS, so agents saw the names while
humans had nowhere to read them.
"""

import re
from pathlib import Path

from jobd.models import KNOWN_EVENTS

_DOC = Path(__file__).resolve().parent.parent / "docs" / "events.md"


def test_every_known_event_is_documented():
    assert _DOC.exists(), "docs/events.md is missing"
    documented = set(re.findall(r"^\| `([a-z_]+)`", _DOC.read_text(), re.M))
    missing = sorted(KNOWN_EVENTS - documented)
    assert not missing, f"undocumented events: {missing}"
    stale = sorted(documented - KNOWN_EVENTS)
    assert not stale, f"docs/events.md lists events KNOWN_EVENTS does not: {stale}"
    # Non-empty floor: an empty doc and an empty constant would agree.
    assert len(documented) >= 30
