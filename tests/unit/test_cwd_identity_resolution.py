"""Precedence for the three resolution rules.

Rule 1 first is the safety property of the whole feature: a submit that
resolves to a registered project today must resolve to the SAME project after
this change, whatever its cwd says. Everything else is a bonus; this is the
invariant.
"""

from jobd.config import ProjectEntry, resolve_effective_config
from jobd.models import JobSubmit


def _req(project: str, cwd: str) -> JobSubmit:
    return JobSubmit(cmd=["true"], cwd=cwd, project=project)


def _projects() -> dict[str, ProjectEntry]:
    return {
        "_default": ProjectEntry(priority=40),
        "beta": ProjectEntry(priority=78, roots=["/home/user/beta"]),
        "gamma": ProjectEntry(priority=60, roots=["/home/user/gamma"]),
    }


def test_rule1_a_registered_name_wins_over_a_conflicting_cwd():
    """THE invariant. Submitted as gamma from inside beta's tree:
    the typed, registered name must win, so no correct submit is repriced."""
    eff = resolve_effective_config(_req("gamma", "/home/user/beta/sweeps"), _projects(), None)
    assert eff.project == "gamma"
    assert eff.priority.value == 60
    assert eff.matched_root is None


def test_rule1_still_wins_when_the_name_needed_folding():
    """A name reaching rule 1 only via case/`-_` folding is still a rule-1 hit
    and must not be overridden by cwd."""
    eff = resolve_effective_config(_req("GAMMA", "/home/user/beta"), _projects(), None)
    assert eff.project == "gamma"
    assert eff.matched_root is None
    assert eff.project_label == "GAMMA"


def test_rule2_an_unregistered_label_takes_its_identity_from_cwd():
    eff = resolve_effective_config(
        _req("pillar2a1_sweep", "/home/user/beta/sweeps"), _projects(), None
    )
    assert eff.project == "beta"
    assert eff.priority.value == 78
    assert eff.matched_root == "/home/user/beta"
    assert eff.project_label == "pillar2a1_sweep"
    assert eff.unknown_project_warning is None


def test_rule2_supplies_project_defaults_too_not_just_priority():
    """Identity means the whole entry, not only the number."""
    projects = _projects()
    projects["beta"].defaults.max_wall_s = 3600
    eff = resolve_effective_config(_req("pillar2a1_sweep", "/home/user/beta"), projects, None)
    assert eff.max_wall_s.value == 3600
    assert eff.max_wall_s.source == "project_default"


def test_rule3_an_unmatched_job_still_warns_as_before():
    """Positive control on the untouched path: the existing unknown_project
    signal must not be weakened by the new one."""
    eff = resolve_effective_config(_req("whatever", "/tmp"), _projects(), None)
    assert eff.project == "whatever"
    assert eff.priority.value == 40
    assert eff.matched_root is None
    assert eff.unknown_project_warning is not None


def test_the_typed_label_is_preserved_in_every_branch():
    projects = _projects()
    for name, cwd in [
        ("gamma", "/home/user/beta"),
        ("pillar2a1_sweep", "/home/user/beta"),
        ("whatever", "/tmp"),
    ]:
        eff = resolve_effective_config(_req(name, cwd), projects, None)
        assert eff.project_label == name
