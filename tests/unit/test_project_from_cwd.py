"""cwd -> project identity matching.

The boundary case is the whole point of this module. A `str.startswith` root
match is the obvious implementation and it is wrong: `/home/mjarnold/jepagame2`
starts with `/home/mjarnold/jepagame`, so a sibling project would silently
inherit its neighbour's priority -- the exact failure class this feature exists
to end, pointing the other way.
"""

from jobd.config import ProjectEntry, project_from_cwd


def _projects(**roots_by_name):
    p = {"_default": ProjectEntry(priority=40)}
    for name, roots in roots_by_name.items():
        p[name.replace("_", "-")] = ProjectEntry(priority=60, roots=roots)
    return p


def test_a_cwd_inside_a_root_resolves_to_that_project():
    projects = _projects(jepagame=["/home/mjarnold/jepagame"])
    assert project_from_cwd(projects, "/home/mjarnold/jepagame/sweeps/a") == (
        "jepagame",
        "/home/mjarnold/jepagame",
    )


def test_the_root_itself_matches():
    projects = _projects(jepagame=["/home/mjarnold/jepagame"])
    assert project_from_cwd(projects, "/home/mjarnold/jepagame") == (
        "jepagame",
        "/home/mjarnold/jepagame",
    )


def test_a_sibling_directory_sharing_a_name_prefix_does_not_match():
    """The regression this module exists to prevent."""
    projects = _projects(jepagame=["/home/mjarnold/jepagame"])
    assert project_from_cwd(projects, "/home/mjarnold/jepagame2") is None
    assert project_from_cwd(projects, "/home/mjarnold/jepagame-scratch") is None


def test_the_deepest_root_wins():
    """Nesting is legitimate: a sub-project living inside a parent's tree must
    take its own identity, not the parent's."""
    projects = _projects(
        jepagame=["/home/mjarnold/jepagame"],
        jepagame_xcheck=["/home/mjarnold/jepagame/xcheck"],
    )
    assert project_from_cwd(projects, "/home/mjarnold/jepagame/xcheck/run1") == (
        "jepagame-xcheck",
        "/home/mjarnold/jepagame/xcheck",
    )


def test_an_unmatched_cwd_returns_none():
    projects = _projects(jepagame=["/home/mjarnold/jepagame"])
    assert project_from_cwd(projects, "/tmp") is None


def test_a_project_with_no_roots_never_matches():
    """Positive control for the opt-in: roots are opt-in, and a project that
    declares none must not be reachable by cwd at all."""
    projects = {"_default": ProjectEntry(priority=40), "plain": ProjectEntry(priority=60)}
    assert project_from_cwd(projects, "/home/mjarnold/plain") is None


def test_two_projects_claiming_the_same_depth_yield_no_identity(caplog):
    """Ambiguity is answered with silence plus a warning, mirroring
    canonical_project_name's posture on an ambiguous fold (config.py:355).
    Picking one would run someone's jobs at a neighbour's priority."""
    projects = _projects(
        alpha=["/home/mjarnold/shared"],
        beta=["/home/mjarnold/shared"],
    )
    with caplog.at_level("WARNING"):
        assert project_from_cwd(projects, "/home/mjarnold/shared/x") is None
    assert "alpha" in caplog.text and "beta" in caplog.text


def test_an_ambiguous_shallow_root_still_loses_to_a_deeper_unambiguous_one():
    """Ambiguity at depth N must not poison a clear winner at depth N+1."""
    projects = _projects(
        alpha=["/home/mjarnold/shared"],
        beta=["/home/mjarnold/shared"],
        gamma=["/home/mjarnold/shared/g"],
    )
    assert project_from_cwd(projects, "/home/mjarnold/shared/g/run") == (
        "gamma",
        "/home/mjarnold/shared/g",
    )


def test_default_is_never_returned_as_an_identity():
    projects = {"_default": ProjectEntry(priority=40, roots=["/home/mjarnold"])}
    assert project_from_cwd(projects, "/home/mjarnold/anything") is None
