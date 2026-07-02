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
    profiles: dict[str, ProfileSpec]
    classifier: list[ClassifierRule]
    paths: dict[str, Path]
    logs_dir: Path
    dispatch_skip_state: dict[int, str]
