"""projects.yaml (de)serialization for the config-mutation endpoints."""

from __future__ import annotations

import os
import threading

import yaml

from jobd.broker.context import BrokerState
from jobd.config import ProjectEntry

# Serializes the /projects mutation endpoints against each other and against
# _persist_projects' iteration (audit 2026-07-15 F7): set/nudge run on the
# threadpool, mutate the shared dict, then iterate it to persist — a concurrent
# insert mid-iteration raises "dictionary changed size", and two concurrent
# persists last-write-win the overlay file.
projects_mutation_lock = threading.Lock()


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
    """Persist runtime priority changes to the mutable overrides overlay.

    audit 2026-07-12: this used to rewrite ``config/projects.yaml`` in place —
    but that file is git-owned and bind-mounted ``:ro`` into the broker, so
    every ``job projects set`` / ``nudge`` raised OSError → HTTP 500 (the
    feature was dead in production). Config ownership is now split: the git
    baseline stays read-only, and only the priority deltas are written here, to
    a writable path next to the DB (see jobd.config for the full rationale).

    Only priorities that DIFFER from the git baseline are written, so a later
    config-as-code priority change still lands for any project nobody nudged.
    Because the mutation endpoints only ever touch ``priority``, ``defaults:``
    blocks are never rewritten — the old "one nudge erases defaults" round-trip
    hazard is now structurally impossible rather than merely tested against.
    """
    baseline = state["base_priorities"]
    overrides = {
        name: entry.priority
        for name, entry in state["projects"].items()
        if baseline.get(name) != entry.priority
    }
    path = state["paths"]["project_overrides"]
    path.parent.mkdir(parents=True, exist_ok=True)
    # tmp + os.replace: a crash mid-write must not leave a truncated overlay —
    # this file is read back at startup/reload, and half a YAML document would
    # take the runtime priorities down with it.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump({"priorities": overrides}, sort_keys=False))
    os.replace(tmp, path)
