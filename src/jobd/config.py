"""YAML config loaders for projects, profiles, classifier rules."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
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

log = logging.getLogger("jobd.config")


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
    or fall through to global broker constants.

    EVERY field uses ``None`` as its "unset" sentinel — including the bools.
    That is load-bearing: `_default.defaults` is a fleet-wide FLOOR merged under
    every project (see `resolve_project_defaults`), and a merge can only tell
    "the project said nothing" from "the project said false" if unset is None.
    `escalate_to_arc: bool = False` would have made those two indistinguishable,
    so a project could never opt out of a floor that set it.
    """

    max_wall_s: int | None = None
    idle_timeout_s: int | None = None
    checkpoint_grace_s: int | None = None
    host_pin: str | None = None  # None means "do not override"
    requires: JobRequires | None = None
    preemptible: bool | None = None  # None means "do not override"
    priority: int | None = None
    escalate_to_arc: bool | None = None


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

# Bounds mirror JobSubmit (models.py) — YAML values bypass Pydantic, and an
# unvalidated value splits broker/worker semantics (audit 2026-07-15 F3):
# `max_wall_s: 0` reads as "unset" to the worker (`or`-falsy) but "enforce" to
# the sweeper (`is not None`), which then orphans a healthy job as
# wall_clock_exceeded; a STRING value TypeErrors the whole sweep pass every
# 30s. Out-of-bounds/mistyped values are dropped loudly, which also means a
# `0` cannot opt a project out of a _default floor — matching the floor's
# existing "None means unset, and unset gets the floor" contract.
_DEFAULTS_BOUNDS: dict[str, tuple[int, int]] = {
    "max_wall_s": (1, 7 * 24 * 3600),
    "idle_timeout_s": (1, 24 * 3600),
    "checkpoint_grace_s": (1, 300),
    "priority": (0, 100),
}
_DEFAULTS_TYPES: dict[str, type] = {
    "host_pin": str,
    "preemptible": bool,
    "escalate_to_arc": bool,
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
        elif v is not None and k in _DEFAULTS_BOUNDS:
            lo, hi = _DEFAULTS_BOUNDS[k]
            if not isinstance(v, int) or isinstance(v, bool) or not (lo <= v <= hi):
                log.warning(
                    "projects.yaml defaults.%s=%r is not an int in [%d, %d] — ignoring it",
                    k,
                    v,
                    lo,
                    hi,
                )
                continue
            kwargs[k] = v
        elif v is not None and k in _DEFAULTS_TYPES and not isinstance(v, _DEFAULTS_TYPES[k]):
            log.warning(
                "projects.yaml defaults.%s=%r is not a %s — ignoring it",
                k,
                v,
                _DEFAULTS_TYPES[k].__name__,
            )
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


# --- runtime priority overrides -------------------------------------------
#
# audit 2026-07-12: `job projects set` / `job projects nudge` used to rewrite
# config/projects.yaml in place. That file is git-owned AND bind-mounted `:ro`
# into the broker container, so every mutation raised OSError → HTTP 500: a
# documented feature was dead in production. Splitting ownership fixes the
# cause rather than widening the mount:
#
#   config/projects.yaml  — STATIC, git-owned, read-only. Declares projects,
#                           their baseline priority, and their `defaults:`.
#   <state>/project-priorities.yaml — MUTABLE runtime state, written by the
#                           broker, living next to the DB (writable, backed up
#                           with it, and never in git's way on a redeploy).
#
# The overlay stores ONLY priorities that differ from the git baseline, so a
# later config-as-code priority change still takes effect for any project that
# was never nudged. And because the endpoints only ever mutate `priority`, the
# `defaults:` blocks stay entirely git-owned — which makes the old
# "one nudge silently erases defaults" round-trip bug structurally impossible.


def load_project_overrides(path: Path | str) -> dict[str, int]:
    """Load the runtime priority-override overlay: {project_name: priority}.

    Missing/empty/malformed-shape file → {} (the overlay is optional state, and
    a broker must always start from its git baseline alone).
    """
    import logging

    log = logging.getLogger("jobd.config")
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return {}

    data = yaml.safe_load(text) or {}
    raw = data.get("priorities", {})
    if not isinstance(raw, dict):
        log.warning("project-priorities overlay at %s has no `priorities:` map; ignoring", path)
        return {}
    out: dict[str, int] = {}
    for name, priority in raw.items():
        try:
            out[str(name)] = max(0, min(100, int(priority)))
        except (TypeError, ValueError):
            log.warning("ignoring non-int priority override for %r in %s", name, path)
    return out


def apply_project_overrides(
    projects: dict[str, ProjectEntry], overrides: dict[str, int]
) -> dict[str, ProjectEntry]:
    """Overlay runtime priority overrides onto the git-baseline projects.

    A project present only in the overlay (created at runtime via
    `job projects set <new-name>`) is materialized with that priority. Mutates
    and returns `projects`.
    """
    for name, priority in overrides.items():
        existing = projects.get(name)
        if existing is None:
            projects[name] = ProjectEntry(priority=priority)
        else:
            existing.priority = priority
    return projects


def load_effective_projects(
    projects_path: Path | str, overrides_path: Path | str
) -> tuple[dict[str, ProjectEntry], dict[str, int]]:
    """Load the git baseline, overlay runtime priority overrides, and return
    (effective_projects, baseline_priorities).

    The caller keeps `baseline_priorities` so `_persist_projects` can write back
    only the genuine deltas.
    """
    baseline = load_projects(projects_path)
    baseline_priorities = {name: entry.priority for name, entry in baseline.items()}
    overrides = load_project_overrides(overrides_path)
    return apply_project_overrides(baseline, overrides), baseline_priorities


def load_profiles(path: Path | str) -> dict[str, ProfileSpec]:
    """Load profiles.yaml into {name: ProfileSpec}.

    Missing file → {}: the README's contract is that all three config files
    are optional, and `load_projects` has honored it since day one — but this
    loader (and the classifier's) hard-crashed instead, so a bare `pip
    install jobd && jobd` on a machine without /app/config died at startup.
    Found by the launch-prep quickstart dry-run in a pristine container;
    invisible before because CI exports JOBD_CONFIG_DIR and the production
    broker runs in Docker where the path exists.
    """
    import logging

    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        logging.getLogger("jobd.config").info(
            "no profiles.yaml found at %s; no profiles defined", path
        )
        return {}
    data = yaml.safe_load(text) or {}
    profiles = data.get("profiles", {})
    out: dict[str, ProfileSpec] = {}
    for name, cfg in profiles.items():
        out[name] = ProfileSpec(name=name, **cfg)
    return out


def load_classifier_rules(path: Path | str) -> list[ClassifierRule]:
    """Load classifier.yaml into list of ClassifierRule.

    Missing file → [] — see load_profiles: the config files are optional by
    documented contract, and a missing one must not stop the broker."""
    import logging

    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        logging.getLogger("jobd.config").info(
            "no classifier.yaml found at %s; no classifier rules", path
        )
        return []
    data = yaml.safe_load(text) or {}
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


def _merge_defaults(floor: ProjectDefaults, over: ProjectDefaults) -> ProjectDefaults:
    """Field-wise merge: `over`'s set fields win, everything else falls to `floor`.

    Derived from `dataclasses.fields`, never a hand-written key list — a new
    field added to ProjectDefaults inherits the floor automatically. Hand-listing
    here is exactly the "forgetting is what fails" shape: the field that gets
    forgotten is the one that silently stops being inherited.
    """
    merged = {}
    for f in fields(ProjectDefaults):
        value = getattr(over, f.name)
        merged[f.name] = value if value is not None else getattr(floor, f.name)
    return ProjectDefaults(**merged)


def resolve_project_defaults(
    projects: dict[str, ProjectEntry], project_name: str
) -> ProjectDefaults:
    """Return the effective defaults for a project: its own, over `_default`'s.

    `_default.defaults` is a FLOOR, not a fallback. It is inherited by EVERY
    project, and a project overrides it one key at a time.

    This used to be `projects.get(name) or projects.get("_default")` — an
    either/or. So `_default.defaults` reached only projects that were NOT in
    projects.yaml, and the moment a project got an entry (even a bare
    `{priority: 60}` written by `job projects set`) it silently lost the whole
    block. The block in question is the zombie hang-guard — idle_timeout_s /
    max_wall_s — which exists *because* a silent job once held a desktop GPU for
    six days. So the guard covered precisely the projects nobody had configured,
    and dropped off the ones anybody cared about. On 2026-07-14, registering 32
    real projects to give them priorities disarmed the hang-guard on all 32,
    including the very project whose zombie created it.

    A default that a project can silently un-inherit is not a default.
    """
    floor = projects.get("_default")
    entry = projects.get(project_name)
    if entry is None:
        return floor.defaults if floor is not None else ProjectDefaults()
    if floor is None or floor is entry:
        return entry.defaults
    return _merge_defaults(floor.defaults, entry.defaults)


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
