"""`_default.defaults` is a FLOOR under every project, not a fallback for unlisted ones.

The bug this file exists to prevent (2026-07-14, production):

    resolve_project_defaults did `projects.get(name) or projects.get("_default")`

— an either/or. A project WITH an entry never saw `_default.defaults`. So the
block called "the FLEET-WIDE hang-guard" in projects.yaml — `idle_timeout_s` and
`max_wall_s`, the reaper that exists because a silent job once held a desktop GPU
for six days — reached only the projects nobody had bothered to configure.

Registering 32 real projects to give them priorities therefore disarmed the
hang-guard on all 32, including orchid-sdxl: the project whose zombie created it.
Nothing failed. No test went red. The guard was simply gone from everything that
mattered, and the config file went on claiming it was fleet-wide.

The tests below assert the semantics that make that unexpressible.
"""

from __future__ import annotations

from dataclasses import MISSING, fields

import pytest

from jobd.config import (
    ProjectDefaults,
    ProjectEntry,
    apply_project_overrides,
    resolve_effective_config,
    resolve_project_defaults,
)
from jobd.models import JobRequires, JobSubmit

# The real fleet floor from config/projects.yaml.
_HANG_GUARD = ProjectDefaults(idle_timeout_s=3600, max_wall_s=172800)


def _fleet(**projects: ProjectEntry) -> dict[str, ProjectEntry]:
    return {"_default": ProjectEntry(priority=40, defaults=_HANG_GUARD), **projects}


# --- the regression -------------------------------------------------------


def test_a_registered_project_still_inherits_the_hang_guard():
    """THE regression. A bare entry — exactly what `job projects set` writes —
    must not strip the fleet guard."""
    projects = _fleet(**{"orchid-sdxl": ProjectEntry(priority=70)})
    d = resolve_project_defaults(projects, "orchid-sdxl")
    assert d.idle_timeout_s == 3600, (
        "a registered project lost the fleet hang-guard. This is the 2026-07-14 "
        "production bug: giving a project a priority disarmed its zombie reaper."
    )
    assert d.max_wall_s == 172800


def test_an_unregistered_project_still_inherits_it_too():
    """The behaviour that always worked must keep working."""
    d = resolve_project_defaults(_fleet(), "never-heard-of-it")
    assert (d.idle_timeout_s, d.max_wall_s) == (3600, 172800)


def test_a_project_overrides_the_floor_one_key_at_a_time():
    """Setting one key must not wipe its siblings — the whole point of a merge."""
    projects = _fleet(
        **{"long-train": ProjectEntry(priority=70, defaults=ProjectDefaults(idle_timeout_s=21600))}
    )
    d = resolve_project_defaults(projects, "long-train")
    assert d.idle_timeout_s == 21600, "the project's own value must win"
    assert d.max_wall_s == 172800, (
        "overriding idle_timeout_s silently dropped max_wall_s — the merge is "
        "replacing the block rather than layering over it"
    )


def test_an_explicit_false_overrides_a_true_floor():
    """Why every field's unset sentinel is None, including the bools.

    With `escalate_to_arc: bool = False`, "the project said nothing" and "the
    project said false" are the same value — so a project could never opt out of
    a floor that turned something on.
    """
    projects = {
        "_default": ProjectEntry(
            priority=40, defaults=ProjectDefaults(preemptible=True, escalate_to_arc=True)
        ),
        "no-checkpoints": ProjectEntry(
            priority=70, defaults=ProjectDefaults(preemptible=False, escalate_to_arc=False)
        ),
    }
    d = resolve_project_defaults(projects, "no-checkpoints")
    assert d.preemptible is False, "a project that says preemptible:false must not be preempted"
    assert d.escalate_to_arc is False


# --- derived coverage: the guard against the next forgotten field ----------


def test_every_field_uses_None_as_its_unset_sentinel():
    """The invariant the merge rests on. Derived from the dataclass, so a field
    added tomorrow is checked without anyone remembering to check it."""
    offenders = [
        f.name
        for f in fields(ProjectDefaults)
        if f.default is not None or f.default_factory is not MISSING
    ]
    assert not offenders, (
        f"{offenders} do not default to None. Every ProjectDefaults field must use None as "
        "'unset', or _merge_defaults cannot tell an unset field from a deliberate zero/false "
        "and the project can never override that key of the fleet floor."
    )


def test_every_field_is_actually_inherited():
    """Set EVERY field on the floor, leave the project bare, demand all of them
    arrive. Enumerated from the dataclass — no hand-written list to forget."""
    full = ProjectDefaults(
        max_wall_s=1,
        idle_timeout_s=2,
        checkpoint_grace_s=3,
        host_pin="gt76",
        requires=JobRequires(gpu=True),
        preemptible=True,
        priority=9,
        escalate_to_arc=True,
    )
    unset = [f.name for f in fields(ProjectDefaults) if getattr(full, f.name) is None]
    assert not unset, f"this test forgot to set {unset}; it cannot prove they are inherited"

    projects = {
        "_default": ProjectEntry(priority=40, defaults=full),
        "bare": ProjectEntry(priority=60),
    }
    d = resolve_project_defaults(projects, "bare")
    dropped = [f.name for f in fields(ProjectDefaults) if getattr(d, f.name) is None]
    assert not dropped, f"the floor did not reach a registered project for: {dropped}"


# --- the production path, end to end --------------------------------------


def test_the_overlay_path_that_actually_broke():
    """`job projects set NEW-NAME 70` materializes a project from the priority
    overlay alone. That is the exact call that disarmed the guard on 32 projects."""
    projects = apply_project_overrides(_fleet(), {"orchid-sdxl": 70, "jepagame": 65})
    for name in ("orchid-sdxl", "jepagame"):
        d = resolve_project_defaults(projects, name)
        assert d.idle_timeout_s == 3600, f"`job projects set {name}` stripped the hang-guard"
        assert projects[name].priority in (70, 65)


@pytest.mark.parametrize("project", ["orchid-sdxl", "never-registered"])
def test_submit_resolves_the_guard_for_registered_and_unregistered_alike(project):
    """Through resolve_effective_config — the cascade /submit and /resolve share.
    A job's idle_timeout must not depend on whether its project has a priority."""
    projects = _fleet(**{"orchid-sdxl": ProjectEntry(priority=70)})
    eff = resolve_effective_config(
        JobSubmit(project=project, cmd=["python", "train.py"], cwd="/tmp"),
        projects,
        profile_spec=None,
    )
    assert eff.idle_timeout_s.value == 3600
    assert eff.idle_timeout_s.source == "project_default"
    assert eff.max_wall_s.value == 172800
