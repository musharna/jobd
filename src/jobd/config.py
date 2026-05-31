"""YAML config loaders for projects, profiles, classifier rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from jobd.models import JobRequires, ProfileSpec


@dataclass
class ClassifierRule:
    id: str
    match_regexes: list[str]
    match_contains: list[str]
    suggest_profile: str
    confidence: Literal["high", "medium", "low"]
    host_aware: bool = False


@dataclass
class ProjectDefaults:
    """Per-project default values for fields that would otherwise be CLI flags
    or fall through to global broker constants."""

    max_wall_s: int | None = None
    idle_timeout_s: int | None = None
    checkpoint_grace_s: int | None = None
    host_pin: str | None = None  # None means "do not override"
    requires: JobRequires | None = None
    preemptible: bool | None = None  # None means "do not override"
    priority: int | None = None
    escalate_to_arc: bool = False


@dataclass
class ProjectEntry:
    """One row of projects.yaml: a base priority plus an optional defaults block."""

    priority: int
    defaults: ProjectDefaults = field(default_factory=ProjectDefaults)


# Keys recognized inside `defaults:` — anything else is silently dropped so
# new fields added later don't break old loaders.
_DEFAULTS_KEYS = {
    "max_wall_s",
    "idle_timeout_s",
    "checkpoint_grace_s",
    "host_pin",
    "requires",
    "preemptible",
    "priority",
    "escalate_to_arc",
}


def _parse_defaults(raw: dict | None) -> ProjectDefaults:
    if not isinstance(raw, dict):
        return ProjectDefaults()
    kwargs: dict = {}
    for k in _DEFAULTS_KEYS:
        if k not in raw:
            continue
        v = raw[k]
        if k == "requires":
            if v is None:
                kwargs["requires"] = None
            elif isinstance(v, dict):
                kwargs["requires"] = JobRequires(**v)
            # any other shape is ignored
        else:
            kwargs[k] = v
    return ProjectDefaults(**kwargs)


def load_projects(path: Path | str) -> dict[str, ProjectEntry]:
    """Load projects.yaml into {name: ProjectEntry}.

    Missing file: log + return a single `_default` entry so the broker can
    still start. Malformed YAML propagates as `yaml.YAMLError`.
    """
    import logging

    log = logging.getLogger("jobd.config")
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        log.info("no projects.yaml found at %s; using global defaults", path)
        return {"_default": ProjectEntry(priority=40)}

    data = yaml.safe_load(text) or {}
    projects = data.get("projects", {})
    out: dict[str, ProjectEntry] = {}
    for name, cfg in projects.items():
        if not isinstance(cfg, dict) or "priority" not in cfg:
            continue
        defaults = _parse_defaults(cfg.get("defaults"))
        out[name] = ProjectEntry(priority=int(cfg["priority"]), defaults=defaults)
    if "_default" not in out:
        out["_default"] = ProjectEntry(priority=40)
    return out


def load_profiles(path: Path | str) -> dict[str, ProfileSpec]:
    """Load profiles.yaml into {name: ProfileSpec}."""
    data = yaml.safe_load(Path(path).read_text()) or {}
    profiles = data.get("profiles", {})
    out: dict[str, ProfileSpec] = {}
    for name, cfg in profiles.items():
        out[name] = ProfileSpec(name=name, **cfg)
    return out


def load_classifier_rules(path: Path | str) -> list[ClassifierRule]:
    """Load classifier.yaml into list of ClassifierRule."""
    data = yaml.safe_load(Path(path).read_text()) or {}
    rules = data.get("rules", [])
    out: list[ClassifierRule] = []
    for r in rules:
        match_regexes: list[str] = []
        match_contains: list[str] = []
        for m in r.get("match", []):
            if "command_regex" in m:
                match_regexes.append(m["command_regex"])
            if "command_contains" in m:
                match_contains.append(m["command_contains"])
        out.append(
            ClassifierRule(
                id=r["id"],
                match_regexes=match_regexes,
                match_contains=match_contains,
                suggest_profile=r["suggest_profile"],
                confidence=r["confidence"],
                host_aware=r.get("host_aware", False),
            )
        )
    return out


def resolve_priority(projects: dict[str, ProjectEntry], name: str, delta: int) -> int:
    """Compute effective priority: project_default + delta, clamped to [0,100].

    Unknown projects fall through to projects['_default'].
    """
    entry = projects.get(name) or projects.get("_default")
    base = entry.priority if entry is not None else 40
    return max(0, min(100, base + delta))


def resolve_profile(profiles: dict[str, ProfileSpec], name: str) -> ProfileSpec | None:
    """Return the named profile, or None if not found."""
    return profiles.get(name)


def resolve_project_defaults(
    projects: dict[str, ProjectEntry], project_name: str
) -> ProjectDefaults:
    """Return ProjectDefaults for project, or empty defaults if absent.

    Falls back to projects['_default'].defaults; if neither is registered,
    returns a zero-valued ProjectDefaults.
    """
    entry = projects.get(project_name) or projects.get("_default")
    if entry is None:
        return ProjectDefaults()
    return entry.defaults
