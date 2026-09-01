"""Replay the real job history through the new resolver.

Two arms, and the SECOND is the one that matters:

  1. the jobs this feature is for now resolve to a project, and
  2. every job that resolves to a registered project today resolves to the
     SAME project, at the SAME priority, after the change.

Arm 2 is the regression control. Without it, a resolver that returned garbage
for everything would still satisfy arm 1 by accident, and a resolver that
returned nothing at all would read as a clean pass.

Both arms go through `resolve_effective_config`, the REAL production entry
point -- not a local mirror of its cascade. A mirror cannot detect a change
to the cascade it mirrors: an earlier version of this test built `_resolve`
out of `canonical_project_name` + `project_from_cwd` directly, which made
arm 2 tautological (`before` and `after` were both `canonical_project_name`
called with the same arguments, so they could never differ) and let arm 1
pass for a resolver that returned pure garbage, as long as it returned
*something*. Fix round 1, 2026-08-31.

Arm 1's "expected project" is ALSO computed independently of `project_from_cwd`
(see `_owning_project` below), for the same reason: if arm 1 asked
`project_from_cwd` what it expects and then checked the resolver's answer
against that same (mutated) function's output, a broken `project_from_cwd`
would corrupt both sides identically and the comparison would still pass.
"""

import csv
from pathlib import Path, PurePosixPath

import pytest

from jobd.config import (
    ProjectEntry,
    canonical_project_name,
    project_from_cwd,
    resolve_effective_config,
    resolve_priority,
)
from jobd.models import JobSubmit

CORPUS = Path(__file__).parent / "data" / "project_cwd_corpus.csv"

# The roots this feature ships with, mirroring config/projects.yaml.
ROOTS = {
    "jepagame": ["/home/mjarnold/jepagame"],
    "orchid-sdxl": ["/home/mjarnold/orchid-sdxl"],
    "dreamer-chassis": ["/home/mjarnold/dreamer-chassis"],
}


@pytest.fixture(scope="module")
def corpus():
    with CORPUS.open() as fh:
        rows = [(r["project"], r["cwd"], int(r["n"])) for r in csv.DictReader(fh)]
    assert len(rows) > 200, f"corpus looks truncated: {len(rows)} rows"
    return rows


@pytest.fixture(scope="module")
def projects():
    p = {"_default": ProjectEntry(priority=40)}
    for name, prio in [
        ("jepagame", 78),
        ("orchid-sdxl", 60),
        ("dreamer-chassis", 60),
        ("agrigen", 80),
    ]:
        p[name] = ProjectEntry(priority=prio, roots=ROOTS.get(name, []))
    return p


def _eff(projects, typed, cwd):
    """The REAL production path. A mirrored cascade cannot detect a change
    to the cascade it mirrors -- that is what made arm 2 a tautology."""
    return resolve_effective_config(
        JobSubmit(cmd=["true"], cwd=cwd, project=typed), projects, None
    )


def _owning_project(projects, cwd):
    """Independent oracle: which registered project's root contains `cwd`,
    computed WITHOUT calling `project_from_cwd`. `ROOTS` above is a flat,
    non-nested, non-overlapping set (three sibling directories), so a plain
    component-wise prefix match is a faithful, independently-written check
    -- it does not need `project_from_cwd`'s deepest-root/ambiguity handling
    to agree with it on this fixture. The point is that a mutation to
    `project_from_cwd` cannot corrupt this oracle, since this function never
    calls it."""
    c = PurePosixPath(cwd).parts
    for name, entry in projects.items():
        if name == "_default":
            continue
        for root in entry.roots:
            r = PurePosixPath(root).parts
            if len(c) >= len(r) and c[: len(r)] == r:
                return name
    return None


def test_arm2_no_currently_registered_submit_changes_priority(corpus, projects):
    """THE regression control. Every job whose typed name resolves to a
    registered project today must keep its exact priority, through the real
    resolver."""
    changed = []
    for typed, cwd, n in corpus:
        before = canonical_project_name(projects, typed)
        if before not in projects:
            continue  # not a rule-1 job; arm 1's business
        eff = _eff(projects, typed, cwd)
        expected_priority = resolve_priority(projects, before, 0)
        if eff.project != before or eff.priority.value != expected_priority:
            changed.append(
                (typed, cwd, before, eff.project, eff.priority.value, expected_priority, n)
            )
    assert not changed, f"{len(changed)} registered submits were repriced: {changed[:5]}"


def test_arm1_jobs_inside_a_rooted_project_now_get_an_identity(corpus, projects):
    """The jobs the feature is for. Counted in JOBS, not distinct pairs: the
    pair count understates by ~15x and would let a near-total failure pass.

    Checks against the SPECIFIC project that owns the matching root (via the
    independent `_owning_project` oracle, not `project_from_cwd` itself), at
    that project's own priority -- not merely that *some* identity came
    back. A resolver returning a fixed bogus project for every cwd would
    satisfy "not None" but must fail this."""
    recovered = 0
    for typed, cwd, n in corpus:
        if canonical_project_name(projects, typed) in projects:
            continue
        expected_project = _owning_project(projects, cwd)
        if expected_project is None:
            continue
        eff = _eff(projects, typed, cwd)
        assert eff.project == expected_project, (
            f"{typed!r}@{cwd!r}: resolved to {eff.project!r}, expected {expected_project!r}"
        )
        expected_priority = resolve_priority(projects, expected_project, 0)
        assert eff.priority.value == expected_priority, (
            f"{typed!r}@{cwd!r}: priority {eff.priority.value} != {expected_priority} "
            f"for {expected_project!r}"
        )
        recovered += n
    assert recovered >= 100, f"only {recovered} jobs recovered; expected 100+"


def test_no_job_is_assigned_an_identity_from_a_scratch_directory(corpus, projects):
    """Positive control on the other side. /tmp and a bare $HOME host many
    projects' work, so an identity derived from either would be a confident
    wrong answer -- worse than the _default it replaced."""
    for typed, cwd, _n in corpus:
        if cwd.rstrip("/") in ("/tmp", "/home/mjarnold", ""):
            assert project_from_cwd(projects, cwd) is None, f"{cwd} claimed by a root"
