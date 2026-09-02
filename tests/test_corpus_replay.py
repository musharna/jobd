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

One honest limit remains, and it belongs here rather than only in a review
ledger: on THIS corpus, arm 2 passes vacuously against a precedence
inversion (rule 2 winning over rule 1) -- 0 of its 1129 rule-1 jobs diverge
under that mutation, because no registered project's real job history has a
cwd crossing into a DIFFERENT registered project's root. People run their
jobs from their own project's directory, so the corpus never exercises the
case arm 2 would need to catch that specific regression. The discrimination
for that regression lives in the synthetic tests instead --
`tests/unit/test_cwd_identity_resolution.py` explicitly, plus
`tests/unit/test_cwd_identity_event.py` and
`tests/unit/test_cwd_identity_resolve_api.py` -- which construct the
crossing case by hand. This file's contribution is real-world BREADTH (249
distinct pairs, 3,608 jobs), not that discrimination. A future refresh of
the CSV could start containing such a row, at which point arm 2 gains real
teeth against a precedence inversion too; nobody should add one by hand to
force that.
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

# Every root this feature ships with, mirroring config/projects.yaml. An
# earlier version of this table listed three of the six while still claiming
# to mirror the shipped config, which quietly narrowed arm 1 from 214 jobs to
# 179: alpha fell through `ROOTS.get(name, [])` to no roots at all, so the
# replay could not have caught a regression in it, and the second root of
# beta and of gamma went unexercised too. (alpha's own tree holds
# 287 corpus jobs, but 264 of them are typed `alpha` exactly and so are
# rule-1 business; 23 are the rule-2 recoveries at stake here.) Add a root
# here whenever one is added there.
ROOTS = {
    "alpha": ["/home/user/alpha"],
    "beta": ["/home/user/beta", "/home/user/beta-1l-sweep"],
    "gamma": ["/home/user/gamma", "/home/user/gamma-data"],
    "delta": ["/home/user/delta"],
}


@pytest.fixture(scope="module")
def corpus():
    with CORPUS.open() as fh:
        rows = [(r["project"], r["cwd"], int(r["n"])) for r in csv.DictReader(fh)]
    assert len(rows) > 200, f"corpus looks truncated: {len(rows)} rows"
    # `_owning_project` below compares path components exactly as written,
    # while production `_path_is_within` first collapses `..` lexically.
    # Those agree today only because no cwd in this corpus contains a `..`
    # segment. A refresh that introduced one would break that agreement
    # silently, so trip loudly here rather than let the oracle drift.
    dotdot = [c for _p, c, _n in rows if ".." in PurePosixPath(c).parts]
    assert not dotdot, (
        f"{len(dotdot)} cwd(s) contain '..' (e.g. {dotdot[0]!r}); teach "
        "`_owning_project` to normalize before trusting it as an oracle"
    )
    return rows


@pytest.fixture(scope="module")
def projects():
    p = {"_default": ProjectEntry(priority=40)}
    for name, prio in [
        ("beta", 78),
        ("gamma", 60),
        ("delta", 60),
        ("alpha", 80),
    ]:
        p[name] = ProjectEntry(priority=prio, roots=ROOTS.get(name, []))
    return p


def _eff(projects, typed, cwd):
    """The REAL production path. A mirrored cascade cannot detect a change
    to the cascade it mirrors -- that is what made arm 2 a tautology."""
    return resolve_effective_config(JobSubmit(cmd=["true"], cwd=cwd, project=typed), projects, None)


def _owning_project(projects, cwd):
    """Independent oracle: which registered project's root contains `cwd`,
    computed WITHOUT calling `project_from_cwd`. `ROOTS` above is a flat,
    non-nested, non-overlapping set (three sibling directories), so a plain
    component-wise prefix match is a faithful, independently-written check
    -- it does not need `project_from_cwd`'s deepest-root/ambiguity handling
    to agree with it on this fixture. The point is that a mutation to
    `project_from_cwd` cannot corrupt this oracle, since this function never
    calls it. It does NOT collapse `..`; the corpus fixture asserts no cwd
    needs that."""
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
    for _typed, cwd, _n in corpus:
        if cwd.rstrip("/") in ("/tmp", "/home/user", ""):
            assert project_from_cwd(projects, cwd) is None, f"{cwd} claimed by a root"
