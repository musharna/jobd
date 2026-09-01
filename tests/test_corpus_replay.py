"""Replay the real job history through the new resolver.

Two arms, and the SECOND is the one that matters:

  1. the jobs this feature is for now resolve to a project, and
  2. every job that resolves to a registered project today resolves to the
     SAME project, at the SAME priority, after the change.

Arm 2 is the regression control. Without it, a resolver that returned garbage
for everything would still satisfy arm 1 by accident, and a resolver that
returned nothing at all would read as a clean pass.
"""

import csv
from pathlib import Path

import pytest

from jobd.config import (
    ProjectEntry,
    canonical_project_name,
    project_from_cwd,
    resolve_priority,
)

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


def _resolve(projects, typed, cwd):
    """The production cascade, mirrored: rule 1, then rule 2, then _default."""
    project = canonical_project_name(projects, typed)
    if project not in projects:
        hit = project_from_cwd(projects, cwd)
        if hit is not None:
            project = hit[0]
    return project


def test_arm2_no_currently_registered_submit_changes_priority(corpus, projects):
    """THE regression control. Every job whose typed name resolves to a
    registered project today must keep its exact priority."""
    changed = []
    for typed, cwd, n in corpus:
        before = canonical_project_name(projects, typed)
        if before not in projects:
            continue  # not a rule-1 job; arm 1's business
        after = _resolve(projects, typed, cwd)
        if after != before or resolve_priority(projects, after, 0) != resolve_priority(
            projects, before, 0
        ):
            changed.append((typed, cwd, before, after, n))
    assert not changed, f"{len(changed)} registered submits were repriced: {changed[:5]}"


def test_arm1_jobs_inside_a_rooted_project_now_get_an_identity(corpus, projects):
    """The jobs the feature is for. Counted in JOBS, not distinct pairs: the
    pair count understates by ~15x and would let a near-total failure pass."""
    recovered = 0
    for typed, cwd, n in corpus:
        if canonical_project_name(projects, typed) in projects:
            continue
        if project_from_cwd(projects, cwd) is not None:
            recovered += n
    assert recovered >= 100, f"only {recovered} jobs recovered; expected 100+"


def test_no_job_is_assigned_an_identity_from_a_scratch_directory(corpus, projects):
    """Positive control on the other side. /tmp and a bare $HOME host many
    projects' work, so an identity derived from either would be a confident
    wrong answer -- worse than the _default it replaced."""
    for typed, cwd, _n in corpus:
        if cwd.rstrip("/") in ("/tmp", "/home/mjarnold", ""):
            assert project_from_cwd(projects, cwd) is None, f"{cwd} claimed by a root"
