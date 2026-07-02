"""projects.yaml (de)serialization for the config-mutation endpoints."""

from __future__ import annotations

import yaml

from jobd.broker.context import BrokerState
from jobd.config import ProjectEntry


def _entry_to_yaml_dict(entry: ProjectEntry) -> dict:
    """Serialize one ProjectEntry to the YAML on-disk shape.

    Critical: emit the ``defaults:`` block whenever it carries non-zero
    values. Older code dropped defaults on every ``set_project_priority``
    or ``nudge_project_priority`` call, silently erasing them after one
    nudge — see docs/projects-yaml.md §8 round-trip canary test.
    """
    out: dict = {"priority": entry.priority}
    d = entry.defaults
    defaults_dict: dict = {}
    if d.max_wall_s is not None:
        defaults_dict["max_wall_s"] = d.max_wall_s
    if d.idle_timeout_s is not None:
        defaults_dict["idle_timeout_s"] = d.idle_timeout_s
    if d.checkpoint_grace_s is not None:
        defaults_dict["checkpoint_grace_s"] = d.checkpoint_grace_s
    if d.host_pin is not None:
        defaults_dict["host_pin"] = d.host_pin
    if d.requires is not None:
        defaults_dict["requires"] = d.requires.model_dump(exclude_none=False)
    if d.preemptible is not None:
        defaults_dict["preemptible"] = d.preemptible
    if d.priority is not None:
        defaults_dict["priority"] = d.priority
    if d.escalate_to_arc:
        defaults_dict["escalate_to_arc"] = d.escalate_to_arc
    if defaults_dict:
        out["defaults"] = defaults_dict
    return out


def _entry_to_jsonable(entry: ProjectEntry) -> dict:
    """Serialize a ProjectEntry to a JSON-safe shape for HTTP responses."""
    return _entry_to_yaml_dict(entry)


def _projects_to_jsonable(projects: dict[str, ProjectEntry]) -> dict[str, dict]:
    return {name: _entry_to_jsonable(entry) for name, entry in projects.items()}


def _persist_projects(state: BrokerState) -> None:
    """Write the in-memory projects dict back to YAML in the canonical shape.

    Round-trip safety: must preserve ``defaults:`` blocks exactly so that a
    single ``job projects nudge`` does not silently erase per-project
    overrides. See test_projects_yaml.test_persist_projects_round_trip.
    """
    data = {
        "projects": {name: _entry_to_yaml_dict(entry) for name, entry in state["projects"].items()}
    }
    state["paths"]["projects"].write_text(yaml.safe_dump(data, sort_keys=False))
