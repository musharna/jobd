"""YAML config loaders for projects, profiles, classifier rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from jobd.models import (
    FieldResolution,
    JobRequires,
    JobSubmit,
    ProfileSpec,
    ResolutionSource,
)


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


@dataclass
class EffectiveConfig:
    """Resolved (value, source) for every field whose precedence is shared by
    POST /submit and POST /resolve. The precedence — CLI > project_default >
    profile > global (docs/projects-yaml.md §3) — lives ONCE here so the two
    endpoints cannot drift (they did: /resolve carried a `!= "any"` guard on
    the profile host_pin branch that /submit lacked).

    `requires.value` is the JobRequires OBJECT (or None): /submit serializes it
    to requires_json, /resolve model_dumps it for the API. `submit`-only fields
    (vram_gb/ram_gb/cpus/fast_path) are NOT here — they have no source label and
    only one consumer, so no duplication to unify. `escalate_to_arc` is reported
    by /resolve and currently ignored by /submit (no Job column)."""

    priority: FieldResolution
    host_pin: FieldResolution
    max_wall_s: FieldResolution
    idle_timeout_s: FieldResolution
    checkpoint_grace_s: FieldResolution
    preemptible: FieldResolution
    requires: FieldResolution  # value: JobRequires | None
    escalate_to_arc: FieldResolution
    unknown_project_warning: str | None


def resolve_effective_config(
    req: JobSubmit,
    projects: dict[str, ProjectEntry],
    profile_spec: ProfileSpec | None,
) -> EffectiveConfig:
    """Resolve every source-tracked field once, applying CLI > project_default
    > profile > global. Callers resolve `profile_spec` themselves (the unknown-
    profile 404 is an HTTP concern) and pass it in.

    The returned FieldResolution objects are what /resolve surfaces verbatim;
    /submit reads only `.value`. This is the single source of truth for the
    precedence cascade both endpoints used to hand-encode separately."""
    proj_defaults = resolve_project_defaults(projects, req.project)
    known_project = req.project in projects

    # priority: value from resolve_priority; source mirrors /resolve's rule
    # (unknown project always attributes to global, even with a CLI delta).
    priority_value = resolve_priority(projects, req.project, req.priority_delta)
    priority_source: ResolutionSource
    if not known_project:
        priority_source = "global"
    elif req.priority_delta != 0:
        priority_source = "cli"
    else:
        priority_source = "project_default"

    # host_pin: the `!= "any"` guard on the profile branch is the divergence
    # this dedup removes — a profile host_hint of "any" is not a real pin, so it
    # must not be attributed to (or override toward) the profile.
    host_pin_source: ResolutionSource
    if req.host_pin != "any":
        host_pin_value = req.host_pin
        host_pin_source = "cli"
    elif proj_defaults.host_pin:
        host_pin_value = proj_defaults.host_pin
        host_pin_source = "project_default"
    elif profile_spec and profile_spec.host_hint and profile_spec.host_hint != "any":
        host_pin_value = profile_spec.host_hint
        host_pin_source = "profile"
    else:
        host_pin_value = "any"
        host_pin_source = "global"

    # max_wall_s / idle_timeout_s / checkpoint_grace_s: CLI > project > global
    # (None). No profile-level default for these.
    max_wall_source: ResolutionSource
    if req.max_wall_s is not None:
        max_wall_value = req.max_wall_s
        max_wall_source = "cli"
    elif proj_defaults.max_wall_s is not None:
        max_wall_value = proj_defaults.max_wall_s
        max_wall_source = "project_default"
    else:
        max_wall_value = None
        max_wall_source = "global"

    idle_source: ResolutionSource
    if req.idle_timeout_s is not None:
        idle_value = req.idle_timeout_s
        idle_source = "cli"
    elif proj_defaults.idle_timeout_s is not None:
        idle_value = proj_defaults.idle_timeout_s
        idle_source = "project_default"
    else:
        idle_value = None
        idle_source = "global"

    ckpt_source: ResolutionSource
    if req.checkpoint_grace_s is not None:
        ckpt_value = req.checkpoint_grace_s
        ckpt_source = "cli"
    elif proj_defaults.checkpoint_grace_s is not None:
        ckpt_value = proj_defaults.checkpoint_grace_s
        ckpt_source = "project_default"
    else:
        ckpt_value = None
        ckpt_source = "global"

    # preemptible: profile only contributes when truthy (matches both prior
    # copies' resolved value; False/None from a profile falls through to global).
    pre_source: ResolutionSource
    if req.preemptible is not None:
        pre_value = req.preemptible
        pre_source = "cli"
    elif proj_defaults.preemptible is not None:
        pre_value = proj_defaults.preemptible
        pre_source = "project_default"
    elif profile_spec is not None and profile_spec.preemptible:
        pre_value = profile_spec.preemptible
        pre_source = "profile"
    else:
        pre_value = False
        pre_source = "global"

    # requires: value is the JobRequires OBJECT (or None); callers serialize.
    req_source: ResolutionSource
    req_value: JobRequires | None
    if req.requires is not None:
        req_value = req.requires
        req_source = "cli"
    elif proj_defaults.requires is not None:
        req_value = proj_defaults.requires
        req_source = "project_default"
    elif profile_spec is not None and profile_spec.requires is not None:
        req_value = profile_spec.requires
        req_source = "profile"
    else:
        req_value = None
        req_source = "global"

    # escalate_to_arc: project_default or global only.
    arc_source: ResolutionSource
    if proj_defaults.escalate_to_arc:
        arc_value = True
        arc_source = "project_default"
    else:
        arc_value = False
        arc_source = "global"

    unknown_project_warning: str | None = None
    if not known_project and "_default" in projects:
        unknown_project_warning = (
            f"project {req.project!r} has no entry in projects.yaml; using global defaults"
        )

    return EffectiveConfig(
        priority=FieldResolution(value=priority_value, source=priority_source),
        host_pin=FieldResolution(value=host_pin_value, source=host_pin_source),
        max_wall_s=FieldResolution(value=max_wall_value, source=max_wall_source),
        idle_timeout_s=FieldResolution(value=idle_value, source=idle_source),
        checkpoint_grace_s=FieldResolution(value=ckpt_value, source=ckpt_source),
        preemptible=FieldResolution(value=pre_value, source=pre_source),
        requires=FieldResolution(value=req_value, source=req_source),
        escalate_to_arc=FieldResolution(value=arc_value, source=arc_source),
        unknown_project_warning=unknown_project_warning,
    )
