"""Shared per-broker state types."""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from jobd.config import ClassifierRule, ProjectEntry
from jobd.models import ProfileSpec


class BrokerState(TypedDict):
    """Shared, mutable per-broker config + scratch, stored on app.state.shared.

    Typed so the heterogeneous values resolve to their real types instead of
    the `object` join mypy would otherwise infer from a bare dict literal.
    """

    projects: dict[str, ProjectEntry]
    # Priorities as declared in the git-owned projects.yaml, before the runtime
    # overrides overlay is applied. `_persist_projects` diffs against this so the
    # overlay only ever carries genuine runtime deltas (audit 2026-07-12).
    base_priorities: dict[str, int]
    profiles: dict[str, ProfileSpec]
    classifier: list[ClassifierRule]
    paths: dict[str, Path]
    logs_dir: Path
    # Dedup key for dispatch_skip events: (job_id, worker_host) -> last reason.
    #
    # It MUST be keyed by worker as well as job. `explain_skip` returns a reason
    # computed against a specific worker, so the same queued job legitimately
    # yields different reasons on different hosts (job 2902: "host_pin" on gt76,
    # "tags" on desktop). Keying by job_id alone made those two answers overwrite
    # each other on every poll, so the "has the reason changed?" guard was true
    # every time and the event fired on every poll from every worker: two
    # permanently-unplaceable jobs emitted 3,908 of 5,340 dispatch_skip events in
    # one sample, ~54k over 7 days, each one a line appended to events.jsonl.
    dispatch_skip_state: dict[tuple[int, str], str]


from collections.abc import Callable  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from typing import TYPE_CHECKING, Any  # noqa: E402

if TYPE_CHECKING:
    import asyncio


@dataclass(frozen=True)
class BrokerDeps:
    """Everything an endpoint closure captures from build_app (Stage-3 split,
    backlog 2026-07-15). Each routes/ module's build_router unpacks these into
    the exact local names the endpoint bodies always used, so the bodies moved
    out of app.py verbatim — the split is a pure move, not a rewrite.
    """

    session_local: Any  # sessionmaker — untyped upstream
    logs_dir: Path
    state: BrokerState
    wake_dispatchers: Callable[[], None]
    # /next-job long-poll plumbing: single-cell holders because the loop only
    # exists after lifespan starts, and the wake event is SWAPPED per wake.
    loop_holder: list[asyncio.AbstractEventLoop | None]
    wake_holder: list[asyncio.Event]
