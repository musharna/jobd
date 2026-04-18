"""YAML config loaders for projects, profiles, classifier rules."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from jobd.models import ProfileSpec


@dataclass
class ClassifierRule:
    id: str
    match_regexes: list[str]
    match_contains: list[str]
    suggest_profile: str
    confidence: Literal["high", "medium", "low"]
    host_aware: bool = False


def load_projects(path: Path | str) -> dict[str, int]:
    """Load projects.yaml into {name: priority} dict."""
    data = yaml.safe_load(Path(path).read_text()) or {}
    projects = data.get("projects", {})
    out: dict[str, int] = {}
    for name, cfg in projects.items():
        if isinstance(cfg, dict) and "priority" in cfg:
            out[name] = int(cfg["priority"])
    if "_default" not in out:
        out["_default"] = 40
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


def resolve_priority(projects: dict[str, int], name: str, delta: int) -> int:
    """Compute effective priority: project_default + delta, clamped to [0,100].

    Unknown projects fall through to projects['_default'].
    """
    base = projects.get(name, projects.get("_default", 40))
    return max(0, min(100, base + delta))


def resolve_profile(profiles: dict[str, ProfileSpec], name: str) -> ProfileSpec | None:
    """Return the named profile, or None if not found."""
    return profiles.get(name)
