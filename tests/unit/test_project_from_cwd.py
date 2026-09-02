"""cwd -> project identity matching.

The boundary case is the whole point of this module. A `str.startswith` root
match is the obvious implementation and it is wrong: `/home/user/beta2`
starts with `/home/user/beta`, so a sibling project would silently
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
    projects = _projects(beta=["/home/user/beta"])
    assert project_from_cwd(projects, "/home/user/beta/sweeps/a") == (
        "beta",
        "/home/user/beta",
    )


def test_the_root_itself_matches():
    projects = _projects(beta=["/home/user/beta"])
    assert project_from_cwd(projects, "/home/user/beta") == (
        "beta",
        "/home/user/beta",
    )


def test_a_sibling_directory_sharing_a_name_prefix_does_not_match():
    """The regression this module exists to prevent."""
    projects = _projects(beta=["/home/user/beta"])
    assert project_from_cwd(projects, "/home/user/beta2") is None
    assert project_from_cwd(projects, "/home/user/beta-scratch") is None


def test_the_deepest_root_wins():
    """Nesting is legitimate: a sub-project living inside a parent's tree must
    take its own identity, not the parent's."""
    projects = _projects(
        beta=["/home/user/beta"],
        beta_xcheck=["/home/user/beta/xcheck"],
    )
    assert project_from_cwd(projects, "/home/user/beta/xcheck/run1") == (
        "beta-xcheck",
        "/home/user/beta/xcheck",
    )


def test_an_unmatched_cwd_returns_none():
    projects = _projects(beta=["/home/user/beta"])
    assert project_from_cwd(projects, "/tmp") is None


def test_a_project_with_no_roots_never_matches():
    """Positive control for the opt-in: roots are opt-in, and a project that
    declares none must not be reachable by cwd at all."""
    projects = {"_default": ProjectEntry(priority=40), "plain": ProjectEntry(priority=60)}
    assert project_from_cwd(projects, "/home/user/plain") is None


def test_two_projects_claiming_the_same_depth_yield_no_identity(caplog):
    """Ambiguity is answered with silence plus a warning, mirroring
    canonical_project_name's posture on an ambiguous fold (config.py:355).
    Picking one would run someone's jobs at a neighbour's priority."""
    projects = _projects(
        alpha=["/home/user/shared"],
        beta=["/home/user/shared"],
    )
    with caplog.at_level("WARNING"):
        assert project_from_cwd(projects, "/home/user/shared/x") is None
    assert "alpha" in caplog.text and "beta" in caplog.text


def test_an_ambiguous_shallow_root_still_loses_to_a_deeper_unambiguous_one():
    """Ambiguity at depth N must not poison a clear winner at depth N+1."""
    projects = _projects(
        alpha=["/home/user/shared"],
        beta=["/home/user/shared"],
        gamma=["/home/user/shared/g"],
    )
    assert project_from_cwd(projects, "/home/user/shared/g/run") == (
        "gamma",
        "/home/user/shared/g",
    )


def test_default_is_never_returned_as_an_identity():
    projects = {"_default": ProjectEntry(priority=40, roots=["/home/user"])}
    assert project_from_cwd(projects, "/home/user/anything") is None


# --- `..` in the submitted cwd ------------------------------------------
#
# `--cwd` is a free-text CLI flag and `JobSubmit.cwd` is a bare `str` with no
# normalization, so a caller can submit a path whose textual components say one
# thing and whose meaning says another. Comparing raw components made a job
# actually running in /tmp price at beta's priority. Collapsing `..`
# LEXICALLY is the fix: the broker has no access to the worker's filesystem, so
# resolving symlinks is both wrong here and unavailable.


def _rooted():
    """The two shipped roots the `..` cases traverse between."""
    projects = {"_default": ProjectEntry(priority=40)}
    projects["beta"] = ProjectEntry(priority=78, roots=["/home/user/beta"])
    projects["gamma"] = ProjectEntry(priority=60, roots=["/home/user/gamma"])
    return projects


def test_dotdot_out_of_a_root_and_into_another_takes_the_other_projects_identity():
    """`/home/user/beta/../gamma` IS gamma. Component-wise
    matching on un-normalized parts read the leading `beta` component and
    handed the job beta's 78."""
    assert project_from_cwd(_rooted(), "/home/user/beta/../gamma") == (
        "gamma",
        "/home/user/gamma",
    )


def test_dotdot_onto_a_prefix_sibling_does_not_match():
    """The sibling-prefix regression, reachable through `..`: this path means
    `/home/user/beta2`, which the direct spelling already refuses."""
    assert project_from_cwd(_rooted(), "/home/user/beta/../beta2") is None


def test_dotdot_escaping_the_home_tree_entirely_does_not_match():
    """The worst case: a job really running in /tmp, priced at beta's 78."""
    assert project_from_cwd(_rooted(), "/home/user/beta/../../tmp") is None
