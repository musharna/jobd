# cwd-derived project identity — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a job's scheduling identity be derived from its `cwd` when the typed `--project` name is not a registered project, so the 241 measured jobs that ran at `_default` 40 inside a registered project's directory get that project's priority.

**Architecture:** `projects.yaml` entries gain a `roots:` list. A new pure function `project_from_cwd` matches a cwd against those roots path-component-wise, longest root winning. `resolve_effective_config` consults it only after `canonical_project_name` fails to find a registered project, so no submit that resolves correctly today can be repriced. The typed name is preserved in a new nullable `Job.project_label` column.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x (SQLite), Pydantic v2, Typer, pytest.

**Spec:** `docs/plans/2026-08-31-cwd-project-identity-design.md` (committed `e95bbc1`)

## Global Constraints

- **Rule 1 wins.** A typed name that resolves to a registered project via `canonical_project_name` is the identity. cwd is consulted **only** when it does not. No currently-correct submit may change priority.
- **Path-component matching, never `str.startswith`.** `/home/mjarnold/jepagame2` must not match root `/home/mjarnold/jepagame`.
- **No symlink resolution.** The broker cannot see the worker's filesystem; it compares the path it was handed.
- **Schema changes go through the existing additive `migrate()`** in `src/jobd/db.py:222` plus an entry in `_JOB_ADDS` (`db.py:184`). No alembic.
- **`cwd_identity_applied` must land in `KNOWN_EVENTS` (`src/jobd/models.py:406`) in the same commit that emits it.** An unlisted name collapses into the `other` bucket at `broker/events.py:74` and any alert on it silently never fires.
- **Every new test is run against pre-change code first and must fail for the stated reason.** A test never seen to fail is not evidence.
- **Branch:** `design/cwd-project-identity`, already checked out, spec committed at `e95bbc1`.
- Run the suite with `.venv/bin/pytest`. It takes ~110-130s; use a generous timeout.
- **Line numbers in this plan are as-of `80e02a3` and drift as tasks land.** Tasks 5 and 6 touch files Task 4 already edited. Locate every edit site by surrounding CONTENT, and treat a cited line number as a hint, not an address.

---

### Task 1: `roots:` on ProjectEntry, parsed and validated

**Files:**

- Modify: `src/jobd/config.py` (`ProjectEntry` at :57, `load_projects` at :136)
- Test: `tests/test_projects_yaml.py`

**Interfaces:**

- Consumes: nothing.
- Produces: `ProjectEntry.roots: list[str]` (normalized, absolute, no trailing slash); `_parse_roots(raw: object, project_name: str) -> list[str]` raising `ValueError` on a bad root.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_projects_yaml.py`:

```python
def test_roots_are_parsed_and_normalized(tmp_path):
    """A trailing slash is a spelling of the same directory, not a different
    root — normalizing at load means the matcher never has to care."""
    p = tmp_path / "projects.yaml"
    p.write_text(
        "projects:\n"
        "  jepagame:\n"
        "    priority: 78\n"
        "    roots:\n"
        "      - /home/mjarnold/jepagame/\n"
        "      - /srv/jepagame\n"
    )
    projects = load_projects(p)
    assert projects["jepagame"].roots == ["/home/mjarnold/jepagame", "/srv/jepagame"]


def test_a_project_without_roots_gets_an_empty_list(tmp_path):
    """Positive control: roots is optional, and its absence must not change
    how an ordinary entry loads."""
    p = tmp_path / "projects.yaml"
    p.write_text("projects:\n  plain: { priority: 60 }\n")
    projects = load_projects(p)
    assert projects["plain"].roots == []
    assert projects["plain"].priority == 60


@pytest.mark.parametrize(
    "bad, why",
    [
        ("roots: [relative/path]", "not absolute"),
        ("roots: [17]", "not a string"),
        ("roots: /home/mjarnold/x", "not a list"),
        ("roots: ['/']", "filesystem root captures every job"),
    ],
)
def test_a_bad_root_is_a_load_error_not_a_silent_drop(tmp_path, bad, why):
    """`defaults:` silently drops unknown keys by design. A dropped ROOT would
    present to the operator as 'the feature just doesn't work on my machine',
    so roots are validated loudly instead."""
    p = tmp_path / "projects.yaml"
    p.write_text(f"projects:\n  jepagame:\n    priority: 78\n    {bad}\n")
    with pytest.raises(ValueError, match="roots"):
        load_projects(p)
```

Ensure `import pytest` and `load_projects` are imported at the top of the file; add them if absent.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_projects_yaml.py -k roots -v`
Expected: FAIL — `AttributeError: 'ProjectEntry' object has no attribute 'roots'` for the first two, and `Failed: DID NOT RAISE` for the parametrized four. Confirm the _reason_ matches; a collection error is not the failure you are looking for.

- [ ] **Step 3: Implement**

In `src/jobd/config.py`, add `roots` to the dataclass (keep the existing docstring line, extend it):

```python
@dataclass
class ProjectEntry:
    """One row of projects.yaml: a base priority, optional defaults, optional roots."""

    priority: int
    defaults: ProjectDefaults = field(default_factory=ProjectDefaults)
    # Absolute directories this project owns. A submit whose typed name matches
    # no registered project takes its identity from the deepest root containing
    # its cwd. Top level rather than inside `defaults:` because a root is not a
    # per-job field default — it is how the project is IDENTIFIED.
    roots: list[str] = field(default_factory=list)
```

Add the parser above `load_projects`:

```python
def _parse_roots(raw: object, project_name: str) -> list[str]:
    """Validate and normalize a project's `roots:` list.

    Raises rather than dropping. `_parse_defaults` silently ignores what it does
    not recognize, which is right for forward-compatible per-job defaults: an
    old broker meeting a new key should keep running. A root is different — it
    is the identity mechanism itself, so a dropped one does not degrade the
    feature, it removes it, and does so invisibly on exactly the machine whose
    config is wrong.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(
            f"projects.yaml {project_name!r}: roots must be a list of absolute paths, "
            f"got {type(raw).__name__}"
        )
    out: list[str] = []
    for r in raw:
        if not isinstance(r, str) or not r.startswith("/"):
            raise ValueError(
                f"projects.yaml {project_name!r}: roots entry {r!r} is not an absolute path"
            )
        norm = str(PurePosixPath(r))
        if norm == "/":
            # A root of "/" contains every cwd, so this one project would claim
            # every otherwise-unidentified job on the fleet -- including /tmp
            # scratch runs. Always a mistake, and a silent one: it produces a
            # confidently wrong identity rather than no identity.
            raise ValueError(
                f"projects.yaml {project_name!r}: roots entry '/' would match every job"
            )
        out.append(norm)
    return out
```

Add `from pathlib import PurePosixPath` to the imports if `PurePosixPath` is not already imported (`Path` is; `PurePosixPath` is separate).

Wire it into `load_projects` — replace the body of the loop at `config.py:154-158`:

```python
    for name, cfg in projects.items():
        if not isinstance(cfg, dict) or "priority" not in cfg:
            continue
        defaults = _parse_defaults(cfg.get("defaults"))
        roots = _parse_roots(cfg.get("roots"), name)
        out[name] = ProjectEntry(
            priority=int(cfg["priority"]), defaults=defaults, roots=roots
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_projects_yaml.py -v`
Expected: PASS, including the pre-existing tests in that file.

- [ ] **Step 5: Commit**

```bash
git add src/jobd/config.py tests/test_projects_yaml.py
git commit -m "feat(config): projects.yaml entries may declare roots"
```

---

### Task 2: `project_from_cwd` — path-component matching, longest root wins

**Files:**

- Modify: `src/jobd/config.py` (new function beside `canonical_project_name` at :335)
- Test: `tests/unit/test_project_from_cwd.py` (create)

**Interfaces:**

- Consumes: `ProjectEntry.roots` from Task 1.
- Produces: `project_from_cwd(projects: dict[str, ProjectEntry], cwd: str) -> tuple[str, str] | None` returning `(project_name, matched_root)` or `None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_project_from_cwd.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_project_from_cwd.py -v`
Expected: FAIL at import — `ImportError: cannot import name 'project_from_cwd'`.

- [ ] **Step 3: Implement**

Add to `src/jobd/config.py`, immediately after `canonical_project_name`:

```python
def _path_is_within(cwd: str, root: str) -> bool:
    """True when `cwd` is `root` or lives underneath it, compared COMPONENT-WISE.

    Not `cwd.startswith(root)`: that says `/home/mjarnold/jepagame2` is inside
    `/home/mjarnold/jepagame`, which would hand a sibling project its
    neighbour's priority. Comparing tuples of path components makes the
    boundary structural rather than textual.
    """
    c = PurePosixPath(cwd).parts
    r = PurePosixPath(root).parts
    return len(c) >= len(r) and c[: len(r)] == r


def project_from_cwd(
    projects: dict[str, ProjectEntry], cwd: str
) -> tuple[str, str] | None:
    """Identify a project by the directory a job runs in.

    Returns (project_name, matched_root), or None when nothing matches or the
    match is ambiguous. The DEEPEST root wins, so a sub-project nested inside a
    parent's tree keeps its own identity.

    `_default` is skipped: it is the fallback priority row, not a project, and
    letting it carry roots would silently convert every unmatched job into a
    confidently-identified one.
    """
    matches: list[tuple[int, str, str]] = []
    for name, entry in projects.items():
        if name == "_default":
            continue
        for root in entry.roots:
            if _path_is_within(cwd, root):
                matches.append((len(PurePosixPath(root).parts), name, root))
    if not matches:
        return None

    best = max(depth for depth, _, _ in matches)
    finalists = sorted({(name, root) for depth, name, root in matches if depth == best})
    distinct_projects = {name for name, _ in finalists}
    if len(distinct_projects) > 1:
        # Two projects claim the same directory, so there is no single right
        # answer. Say so and yield nothing, exactly as canonical_project_name
        # does for an ambiguous fold: guessing here runs someone's jobs at a
        # neighbour's priority, which is the failure this matching prevents.
        log.warning(
            "cwd %r is claimed by more than one project (%s) — no cwd identity "
            "applied; narrow their roots or merge the projects",
            cwd,
            ", ".join(sorted(distinct_projects)),
        )
        return None
    return finalists[0]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_project_from_cwd.py -v`
Expected: PASS, 9 tests.

- [ ] **Step 5: Commit**

```bash
git add src/jobd/config.py tests/unit/test_project_from_cwd.py
git commit -m "feat(config): match a cwd to a project by its roots"
```

---

### Task 3: Wire cwd identity into the resolution cascade

**Files:**

- Modify: `src/jobd/config.py` (`EffectiveConfig` at :459-471, `resolve_effective_config` at :475)
- Test: `tests/unit/test_cwd_identity_resolution.py` (create)

**Interfaces:**

- Consumes: `project_from_cwd` from Task 2.
- Produces: `EffectiveConfig.project_label: str` (the name as typed) and `EffectiveConfig.matched_root: str | None` (the root that supplied the identity, `None` when cwd was not consulted or did not match). `EffectiveConfig.project` keeps its existing meaning: the scheduling identity.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_cwd_identity_resolution.py`:

```python
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
        "jepagame": ProjectEntry(priority=78, roots=["/home/mjarnold/jepagame"]),
        "orchid-sdxl": ProjectEntry(priority=60, roots=["/home/mjarnold/orchid-sdxl"]),
    }


def test_rule1_a_registered_name_wins_over_a_conflicting_cwd():
    """THE invariant. Submitted as orchid-sdxl from inside jepagame's tree:
    the typed, registered name must win, so no correct submit is repriced."""
    eff = resolve_effective_config(
        _req("orchid-sdxl", "/home/mjarnold/jepagame/sweeps"), _projects(), None
    )
    assert eff.project == "orchid-sdxl"
    assert eff.priority.value == 60
    assert eff.matched_root is None


def test_rule1_still_wins_when_the_name_needed_folding():
    """A name reaching rule 1 only via case/`-_` folding is still a rule-1 hit
    and must not be overridden by cwd."""
    eff = resolve_effective_config(
        _req("Orchid_SDXL", "/home/mjarnold/jepagame"), _projects(), None
    )
    assert eff.project == "orchid-sdxl"
    assert eff.matched_root is None
    assert eff.project_label == "Orchid_SDXL"


def test_rule2_an_unregistered_label_takes_its_identity_from_cwd():
    eff = resolve_effective_config(
        _req("pillar2a1_sweep", "/home/mjarnold/jepagame/sweeps"), _projects(), None
    )
    assert eff.project == "jepagame"
    assert eff.priority.value == 78
    assert eff.matched_root == "/home/mjarnold/jepagame"
    assert eff.project_label == "pillar2a1_sweep"
    assert eff.unknown_project_warning is None


def test_rule2_supplies_project_defaults_too_not_just_priority():
    """Identity means the whole entry, not only the number."""
    projects = _projects()
    projects["jepagame"].defaults.max_wall_s = 3600
    eff = resolve_effective_config(
        _req("pillar2a1_sweep", "/home/mjarnold/jepagame"), projects, None
    )
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
        ("orchid-sdxl", "/home/mjarnold/jepagame"),
        ("pillar2a1_sweep", "/home/mjarnold/jepagame"),
        ("whatever", "/tmp"),
    ]:
        eff = resolve_effective_config(_req(name, cwd), projects, None)
        assert eff.project_label == name
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_cwd_identity_resolution.py -v`
Expected: FAIL — `AttributeError: 'EffectiveConfig' object has no attribute 'matched_root'`. `test_rule1_*` and `test_rule3_*` may pass already; that is correct and expected, they are the controls asserting existing behaviour is unchanged.

- [ ] **Step 3: Implement**

In `EffectiveConfig` (`config.py`), add two fields after the existing `project: str`:

```python
    project: str
    # The name exactly as the caller typed it. `project` above may differ: by
    # folding (rule 1), or because cwd supplied the identity (rule 2). Kept so
    # the Job row can record what a human will search for.
    project_label: str
    # The root that supplied the identity, when cwd did. None when the typed
    # name was already registered, or when nothing matched.
    matched_root: str | None
```

In `resolve_effective_config`, replace the three-line resolution block at `config.py:490-492`:

```python
    # Rule 1: a typed name that IS a registered project (after folding) is the
    # identity. Rule 2: otherwise the cwd's deepest owning root supplies one.
    # Rule 3: otherwise `_default`, and unknown_project still warns below.
    #
    # Rule 1 must be tried first and must win. It is what makes this safe to
    # deploy: every submit that resolves correctly today resolves identically
    # after this, whatever its cwd says. cwd only ever fills a vacancy.
    project_label = req.project
    project = canonical_project_name(projects, project_label)
    matched_root: str | None = None
    if project not in projects:
        hit = project_from_cwd(projects, req.cwd)
        if hit is not None:
            project, matched_root = hit
    proj_defaults = resolve_project_defaults(projects, project)
    known_project = project in projects
```

Add both new fields to the `EffectiveConfig(...)` construction at the end of the function:

```python
        project=project,
        project_label=project_label,
        matched_root=matched_root,
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_cwd_identity_resolution.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Run the full suite — this is the blast-radius check**

Run: `.venv/bin/pytest -q` (timeout 600s)
Expected: PASS. `EffectiveConfig` is constructed in one place but read in several; a failure here names a consumer the plan missed. Fix any construction sites that pass positional arguments.

- [ ] **Step 6: Commit**

```bash
git add src/jobd/config.py tests/unit/test_cwd_identity_resolution.py
git commit -m "feat(config): derive project identity from cwd when the typed name is unregistered"
```

---

### Task 4: Record the typed label on the Job row

**Files:**

- Modify: `src/jobd/db.py` (`Job` at :33, `_JOB_ADDS` at :184)
- Modify: `src/jobd/broker/submit.py` (:84, and the three `project=project` insert sites at :251, :343, :355)
- Test: `tests/unit/test_project_label_column.py` (create)

**Interfaces:**

- Consumes: `EffectiveConfig.project_label` from Task 3.
- Produces: `Job.project_label: str | None` — the typed name when it differs from `Job.project`, else `NULL`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_project_label_column.py`:

```python
"""The label column, including the migration path for a live DB.

NULL means "same as project". That choice is what makes this a pure ALTER with
no backfill: every one of the 3,608 existing rows is already correct under it.
"""

from sqlalchemy import create_engine, inspect, text

from jobd.db import Base, migrate


def test_migrate_adds_project_label_to_a_pre_existing_table(tmp_path):
    """The real-execution check for the migration: build a jobs table WITHOUT
    the column, run migrate, and read the live schema back. A test that only
    creates a fresh table via metadata would never exercise the ALTER."""
    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE jobs DROP COLUMN project_label"))
    assert "project_label" not in {c["name"] for c in inspect(engine).get_columns("jobs")}

    migrate(engine)

    cols = {c["name"] for c in inspect(engine).get_columns("jobs")}
    assert "project_label" in cols


def test_migrate_is_idempotent(tmp_path):
    """Positive control: migrate runs on every broker start, so a second pass
    must not raise 'duplicate column name'."""
    url = f"sqlite:///{tmp_path / 'x.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    migrate(engine)
    migrate(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("jobs")}
    assert "project_label" in cols
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_project_label_column.py -v`
Expected: FAIL — `OperationalError: no such column: "project_label"` from the `DROP COLUMN` in the first test (the column does not exist yet), and the second's final assert fails.

- [ ] **Step 3: Implement**

In `src/jobd/db.py`, add the column to `Job` immediately after `project`:

```python
    project: Mapped[str] = mapped_column(String(100), index=True)
    # The name as the submitter typed it, when it differs from `project` above.
    # `project` is the SCHEDULING identity (which projects.yaml row prices this
    # job); this is the free-text run label a human searches for. NULL means the
    # two are the same, which is why this needed no backfill: every row that
    # predates the column is already correct under that reading.
    project_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
```

Add to `_JOB_ADDS` (append to the list, `db.py:184`):

```python
    ("project_label", "VARCHAR(100)"),
```

In `src/jobd/broker/submit.py`, after `project = eff.project` at :84:

```python
    project = eff.project
    # NULL when the label and the identity agree, so the column carries only
    # genuine divergence and `job list --project X` on a plain submit still
    # matches on `project` alone.
    project_label = eff.project_label if eff.project_label != project else None
```

Add `project_label=project_label,` beside each of the three `project=project,` arguments in the `Job(...)` constructions (:251, :343, :355). Leave the `_emit_event(..., project=project, ...)` calls alone — those take the identity.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_project_label_column.py -v`
Expected: PASS, 2 tests.

- [ ] **Step 5: Commit**

```bash
git add src/jobd/db.py src/jobd/broker/submit.py tests/unit/test_project_label_column.py
git commit -m "feat(db): record the typed project label beside the scheduling identity"
```

---

### Task 5: Emit `cwd_identity_applied`

**Files:**

- Modify: `src/jobd/models.py` (`KNOWN_EVENTS` at :406)
- Modify: `src/jobd/broker/submit.py` (beside the `typed_warnings` emission loop at :362)
- Test: `tests/unit/test_cwd_identity_event.py` (create)

**Interfaces:**

- Consumes: `EffectiveConfig.matched_root` from Task 3.
- Produces: event `cwd_identity_applied`, payload `{project_label, matched_root}`, `project` = the resolved identity.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_cwd_identity_event.py`:

```python
"""The event, and the registration that makes it observable.

The second test is the one that matters. An event name missing from
KNOWN_EVENTS still reaches events.jsonl, so a test that only greps the log file
passes while the Prometheus counter buckets it as "other" -- and an alert
written against the real name never fires, looking healthy the whole time.
That is why the registration gets its own assertion.
"""

from jobd.models import KNOWN_EVENTS


def test_cwd_identity_applied_is_a_known_event():
    """Not decoration: broker/events.py:74 buckets any unlisted name as
    'other', which silently disarms every alert written against it."""
    assert "cwd_identity_applied" in KNOWN_EVENTS


def test_the_event_is_emitted_with_the_label_and_the_root(rooted_logs):
    import json

    client, logs_dir = rooted_logs
    r = client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/home/mjarnold/jepagame/sweeps",
            "project": "pillar2a1_sweep",
        },
    )
    assert r.status_code == 200, r.text
    rows = [
        json.loads(line)
        for line in (logs_dir / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    hits = [e for e in rows if e["event"] == "cwd_identity_applied"]
    assert len(hits) == 1, f"expected exactly one, got {[e['event'] for e in rows]}"
    assert hits[0]["project"] == "jepagame"
    assert hits[0]["payload"]["project_label"] == "pillar2a1_sweep"
    assert hits[0]["payload"]["matched_root"] == "/home/mjarnold/jepagame"


def test_no_event_when_the_typed_name_was_already_registered(rooted_logs):
    """Positive control. Without this, an implementation that fires the event
    on EVERY submit would pass the test above and make the metric useless."""
    import json

    client, logs_dir = rooted_logs
    r = client.post(
        "/submit",
        json={"cmd": ["true"], "cwd": "/home/mjarnold/jepagame", "project": "jepagame"},
    )
    assert r.status_code == 200, r.text
    rows = [
        json.loads(line)
        for line in (logs_dir / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert not [e for e in rows if e["event"] == "cwd_identity_applied"]
```

These tests need a projects table with roots, which the shared
`sample_projects_yaml` fixture (`tests/conftest.py:100`) does not have — and its
own docstring says narrower tests should bring their own. Do NOT edit the shared
one: `tests/test_api.py` asserts its exact priorities (`project-a` 55,
`project-b` 80) in a dozen places.

Add this to `tests/unit/conftest.py` so Task 6 can reuse it:

```python
import pytest
from starlette.testclient import TestClient

from jobd.app import build_app


@pytest.fixture
def rooted_app(tmp_path, sample_profiles_yaml, sample_classifier_yaml):
    """An app whose projects table declares roots.

    Separate from `sample_projects_yaml` on purpose: that fixture's exact
    priorities are asserted across tests/test_api.py, so widening it to carry
    roots would couple this feature's tests to every one of those.
    """
    projects = tmp_path / "rooted-projects.yaml"
    projects.write_text(
        "projects:\n"
        "  jepagame:\n"
        "    priority: 78\n"
        "    roots: ['/home/mjarnold/jepagame']\n"
        "  orchid-sdxl:\n"
        "    priority: 60\n"
        "    roots: ['/home/mjarnold/orchid-sdxl']\n"
        "  _default: { priority: 40 }\n"
    )
    return build_app(
        db_url=f"sqlite:///{tmp_path}/rooted.db",
        projects_path=projects,
        profiles_path=sample_profiles_yaml,
        classifier_path=sample_classifier_yaml,
        logs_path=tmp_path / "logs",
    )


@pytest.fixture
def rooted_client(rooted_app):
    return TestClient(rooted_app)


@pytest.fixture
def rooted_logs(rooted_client, tmp_path):
    """(client, logs_dir), mirroring the shared `client_logs` pair."""
    return rooted_client, tmp_path / "logs"
```

The import path is `jobd.app` (verified against `tests/conftest.py:9`), not `jobd.broker.app`.
Note that a submit with a cwd no registered worker can see produces a
`cwd_route_warning` — that is expected in tests and does not fail the submit.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_cwd_identity_event.py -v`
Expected: FAIL — `assert 'cwd_identity_applied' in KNOWN_EVENTS` fails outright; the emission tests fail with `len(hits) == 0`. The third test (the control) passes already, which is correct.

- [ ] **Step 3: Implement**

In `src/jobd/models.py`, add to `KNOWN_EVENTS` after the warning-kind block:

```python
        # cwd supplied a job's scheduling identity because the typed name was
        # not a registered project. Distinct from `unknown_project` (which is
        # now the strictly-worse case: nothing identified the job at all), so a
        # rising rate here reads as "roots are working" while a rising rate
        # there still reads as "someone's work is being priced at 40".
        "cwd_identity_applied",
```

In `src/jobd/broker/submit.py`, inside the `for jid in member_ids:` loop,
immediately after the `job_submitted` emission and **OUTSIDE** the
`if warning_text:` block beneath it.

Placement is the whole point here, so locate it by content rather than by line
number (Task 4 has already shifted this file). The typed-warning emissions are
nested inside `if warning_text:`. Putting this event there would be a defect
that hides itself: when rule 2 fires, `unknown_project_warning` is None, so a
plain cwd-identified submit usually has **no** warning text at all — the event
would never fire in exactly the case it exists to report, while still firing on
the rarer submit that happened to warn for some other reason.

```python
        for jid in member_ids:
            _emit_event(
                logs_dir,
                "job_submitted",
                ...
            )
            # Not a warning -- nothing went wrong -- so this sits beside
            # job_submitted rather than inside the `if warning_text:` block
            # below. Per member, like job_submitted: every member of an array
            # shares the identity, and a per-job count is what the metric means.
            if eff.matched_root is not None:
                _emit_event(
                    logs_dir,
                    "cwd_identity_applied",
                    source="broker",
                    job_id=jid,
                    project=project,
                    project_label=eff.project_label,
                    matched_root=eff.matched_root,
                )
            if warning_text:
                ...
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_cwd_identity_event.py -v`
Expected: PASS, 3 tests.

- [ ] **Step 5: Check the schema-parity tests still pass**

Run: `.venv/bin/pytest tests/mcp/test_events_schema_parity.py tests/test_metrics_event_counter.py tests/unit/test_warning_events.py -v`
Expected: PASS. These three derive from `KNOWN_EVENTS`; one of them may assert an exact set or count and need the new name added.

- [ ] **Step 6: Commit**

```bash
git add src/jobd/models.py src/jobd/broker/submit.py tests/unit/test_cwd_identity_event.py
git commit -m "feat(events): emit cwd_identity_applied when cwd supplies the identity"
```

---

### Task 6: Surface it — `/resolve`, the submit echo, and the list filter

**Files:**

- Modify: `src/jobd/models.py` (`ResolvedConfig`, `JobInfo` at :200)
- Modify: `src/jobd/broker/routes/config.py` (`resolve_job` at :121-155)
- Modify: `src/jobd/broker/routes/jobs.py` (:107-108)
- Modify: `src/jobd/broker/jobinfo.py` (`_to_info` at :84)
- Modify: `src/jobd/broker/submit.py` (the dry-run `validation` block at :226-240)
- Modify: `src/job_cli/cli.py` (the submit echo; `list` at :603)
- Test: `tests/unit/test_list_filter_matches_either_name.py` (create), `tests/unit/test_cwd_identity_resolve_api.py` (create)

**Interfaces:**

- Consumes: `EffectiveConfig.matched_root`, `Job.project_label`.
- Produces: `ResolvedConfig.matched_root: str | None`, `ResolvedConfig.project_label: str`, `JobInfo.project_label: str | None`, and `effective_project` / `effective_matched_root` in the dry-run `validation` dict.

**Note on the submit response.** `/submit` returns three different shapes — the
dry-run dict (`submit.py:221`), the array dict (`:381`), and `_to_info(...)` for
a single job (`:388`). None carries `matched_root`, so the CLI cannot read one
from the submit response. The single-job echo therefore compares
`JobInfo.project_label` against `JobInfo.project`, which is already on the row;
the dry-run path gets explicit `effective_project` / `effective_matched_root`
keys because the deployment check in Task 8 reads them.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_list_filter_matches_either_name.py`:

```python
"""`--project` must find work under either name.

A filter that matched only the identity would lose every historical query a
human has muscle memory for; one that matched only the label would stop finding
a project's own jobs. Both, or the split is a regression in daily use.
"""


def test_filtering_by_the_label_finds_the_job(rooted_client):
    client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/home/mjarnold/jepagame/sweeps",
            "project": "pillar2a1_sweep",
        },
    )
    rows = client.get("/jobs", params={"project": "pillar2a1_sweep"}).json()["jobs"]
    assert len(rows) == 1
    assert rows[0]["project"] == "jepagame"


def test_filtering_by_the_identity_finds_the_same_job(rooted_client):
    client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/home/mjarnold/jepagame/sweeps",
            "project": "pillar2a1_sweep",
        },
    )
    rows = client.get("/jobs", params={"project": "jepagame"}).json()["jobs"]
    assert len(rows) == 1


def test_an_unrelated_project_filter_still_matches_nothing(rooted_client):
    """Positive control: OR-ing two columns is exactly how a filter
    accidentally becomes a no-op that matches everything."""
    client.post(
        "/submit",
        json={
            "cmd": ["true"],
            "cwd": "/home/mjarnold/jepagame/sweeps",
            "project": "pillar2a1_sweep",
        },
    )
    assert client.get("/jobs", params={"project": "orchid-sdxl"}).json()["jobs"] == []
```

Create `tests/unit/test_cwd_identity_resolve_api.py`:

```python
def test_resolve_reports_the_root_that_supplied_the_identity(rooted_client):
    """/resolve is the dry-run surface: if it cannot say WHY a project was
    chosen, an operator debugging a surprise priority has nothing to read."""
    r = client.post(
        "/resolve",
        json={
            "cmd": ["true"],
            "cwd": "/home/mjarnold/jepagame/sweeps",
            "project": "pillar2a1_sweep",
        },
    )
    body = r.json()
    assert body["project"] == "jepagame"
    assert body["project_label"] == "pillar2a1_sweep"
    assert body["matched_root"] == "/home/mjarnold/jepagame"


def test_resolve_reports_no_root_for_a_registered_name(rooted_client):
    r = client.post(
        "/resolve",
        json={"cmd": ["true"], "cwd": "/home/mjarnold/jepagame", "project": "jepagame"},
    )
    assert r.json()["matched_root"] is None
```

Both files use the `rooted_client` fixture added in Task 5. In the test
bodies, `client` is now `rooted_client` — rename the local calls accordingly.
The `/resolve` tests belong in `tests/test_api.py`, which does not see
`tests/unit/conftest.py`; put them in `tests/unit/test_cwd_identity_resolve_api.py`
instead so they can reach the fixture.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_list_filter_matches_either_name.py tests/unit/test_cwd_identity_resolve_api.py -v`
Expected: FAIL — `KeyError: 'matched_root'` on the `/resolve` tests; `assert len(rows) == 1` fails with `0` on the label filter. `test_an_unrelated_project_filter_still_matches_nothing` and `test_filtering_by_the_identity_finds_the_same_job` pass already — they are controls.

- [ ] **Step 3: Implement**

In `src/jobd/models.py`, add to `ResolvedConfig`:

```python
    project_label: str
    matched_root: str | None = None
```

In `src/jobd/broker/routes/config.py`, in the `ResolvedConfig(...)` construction:

```python
            project=eff.project,
            project_label=eff.project_label,
            matched_root=eff.matched_root,
```

In `src/jobd/broker/routes/jobs.py`, replace the filter at :107-108:

```python
            if project:
                # Either name: the identity that priced the job, or the label
                # the submitter typed. A human filtering by the label they used
                # must still find their run after cwd supplied a different
                # identity for it.
                conds.append(
                    or_(Job.project == project, Job.project_label == project)
                )
```

Add `or_` to the `sqlalchemy` import in that file if absent.

In `src/jobd/models.py`, add to `JobInfo` after `project`:

```python
    project: str
    # The name the submitter typed, when it differs from `project`. None when
    # they agree. Nullable rather than always-populated so the field reads as
    # "something was substituted here", which is the only case worth showing.
    project_label: str | None = None
```

In `src/jobd/broker/jobinfo.py`, pass it through in `_to_info`:

```python
        project=job.project,
        project_label=job.project_label,
```

In `src/jobd/broker/submit.py`, add to the dry-run `validation` dict (:226-240),
beside the other `effective_*` keys:

```python
                    "effective_project": project,
                    "effective_matched_root": eff.matched_root,
```

In `src/job_cli/cli.py`, in the single-job submit result handler, after the
existing warning output:

```python
    label = body.get("project_label")
    if label and label != body.get("project"):
        typer.echo(f"project: {label} -> {body['project']}  (identity from cwd)")
```

Use whatever local variable already holds the submit response body at that
point; do not introduce a second request. The root is deliberately not echoed
here — the submit response does not carry it, and `/resolve` plus the
`cwd_identity_applied` event are where the full explanation lives.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_list_filter_matches_either_name.py tests/unit/test_cwd_identity_resolve_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/jobd/models.py src/jobd/broker/routes/config.py src/jobd/broker/routes/jobs.py src/job_cli/cli.py tests/
git commit -m "feat(api): report the cwd-derived identity and filter on either name"
```

---

### Task 7: Corpus replay against the production job history

**Files:**

- Create: `scripts/export_project_cwd_corpus.py`
- Create: `tests/data/project_cwd_corpus.csv`
- Create: `tests/test_corpus_replay.py`
- Test: `tests/test_corpus_replay.py`

**Interfaces:**

- Consumes: `project_from_cwd`, `canonical_project_name`, `resolve_priority` from Tasks 1-3.
- Produces: nothing downstream; this is the acceptance gate.

**Why a committed CSV rather than a live DB query:** the live DB lives on the broker host and CI cannot reach it, but a hand-written fixture would encode the author's beliefs about the corpus rather than the corpus — which is exactly how the v0.5.40 CLI bug shipped. A verbatim export, refreshable by a committed script, is production data that CI can replay.

- [ ] **Step 1: Write the exporter**

Create `scripts/export_project_cwd_corpus.py`:

```python
#!/usr/bin/env python3
"""Export distinct (project, cwd, count) from the live job history.

Run on the broker host; commit the output. This is production data copied
verbatim, not a fixture: the whole value of the replay test is that the author
did not choose its contents.

    ssh <broker> 'python3 - < scripts/export_project_cwd_corpus.py' \\
        > tests/data/project_cwd_corpus.csv
"""

import csv
import os
import signal
import sqlite3
import sys

signal.signal(
    signal.SIGALRM,
    lambda *_: (sys.stderr.write("aborting: walltime guard\n"), sys.exit(2)),
)
signal.alarm(60)

DB = os.environ.get("JOBD_DB", "/home/mjarnold/jobd/data/jobd.db")
rows = sqlite3.connect(f"file:{DB}?mode=ro", uri=True).execute(
    "SELECT project, cwd, COUNT(*) FROM jobs GROUP BY project, cwd ORDER BY project, cwd"
).fetchall()

w = csv.writer(sys.stdout)
w.writerow(["project", "cwd", "n"])
w.writerows(rows)
```

- [ ] **Step 2: Generate the corpus**

```bash
mkdir -p tests/data
ssh mjarnold@100.113.204.41 'timeout 90 python3 -' \
  < scripts/export_project_cwd_corpus.py > tests/data/project_cwd_corpus.csv
wc -l tests/data/project_cwd_corpus.csv   # expect ~250 (249 pairs + header)
```

If the row count is far from 250, stop and investigate before continuing — the corpus is the evidence base for the whole feature.

- [ ] **Step 3: Write the failing test**

Create `tests/test_corpus_replay.py`:

```python
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
        if after != before or resolve_priority(
            projects, after, 0
        ) != resolve_priority(projects, before, 0):
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
```

- [ ] **Step 4: Run the test to verify it fails on pre-change code**

```bash
git stash                                        # revert to pre-feature source
.venv/bin/pytest tests/test_corpus_replay.py -v  # expect ImportError: project_from_cwd
git stash pop
```

Expected on the stashed tree: FAIL with `ImportError: cannot import name 'project_from_cwd'`. Then, on the restored tree, arm 1 must be the test that fails if you delete the `roots` from the `projects` fixture — check that too, since arm 1 passing for the wrong reason is the likelier defect.

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_corpus_replay.py -v`
Expected: PASS, 3 tests. If arm 1 reports fewer than 100 recovered jobs, the `ROOTS` table is missing a directory the corpus shows matters — cross-check against the spec's top-entries table before loosening the threshold.

- [ ] **Step 6: Commit**

```bash
git add scripts/export_project_cwd_corpus.py tests/data/project_cwd_corpus.csv tests/test_corpus_replay.py
git commit -m "test: replay the production job corpus through the cwd resolver"
```

---

### Task 8: Ship the roots and document them

**Files:**

- Modify: `config/projects.yaml`
- Modify: `docs/projects-yaml.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add roots to the projects the corpus shows need them**

In `config/projects.yaml`, add a `roots:` list to each project that owns a directory. Derive the list from the corpus, not from memory:

```bash
.venv/bin/python - <<'PY'
import csv, collections
rows = list(csv.DictReader(open("tests/data/project_cwd_corpus.csv")))
by_cwd = collections.Counter()
for r in rows:
    by_cwd[r["cwd"]] += int(r["n"])
for cwd, n in by_cwd.most_common(30):
    print(f"{n:6d}  {cwd}")
PY
```

Add a root only where the directory unambiguously belongs to one project. Skip `/tmp`, bare `$HOME`, and any directory the corpus shows serving several unrelated projects — those are exactly the cases where an identity would be a confident wrong answer.

- [ ] **Step 2: Verify the shipped config loads and matches as intended**

```bash
.venv/bin/python - <<'PY'
from jobd.config import load_projects, project_from_cwd
p = load_projects("config/projects.yaml")
for name, e in sorted(p.items()):
    if e.roots:
        print(f"{name:30s} {e.roots}")
print()
for cwd in ["/home/mjarnold/jepagame/sweeps", "/home/mjarnold/jepagame2", "/tmp"]:
    print(f"{cwd:40s} -> {project_from_cwd(p, cwd)}")
PY
```

Expected: the roots print as declared; `/home/mjarnold/jepagame/sweeps` resolves to `jepagame`; `/home/mjarnold/jepagame2` and `/tmp` resolve to `None`. A load error here means a malformed root — fix it now rather than at broker start.

- [ ] **Step 3: Document `roots:` in `docs/projects-yaml.md`**

Add a section covering: the YAML shape; the three-rule resolution order with rule 1's precedence stated as the safety property; longest-root-wins; that matching is path-component-wise and does not resolve symlinks; that two projects claiming one directory yields no identity plus a warning; and that `--project` remains required and is now recorded as the run label.

- [ ] **Step 4: Add a CHANGELOG entry**

Under the unreleased heading, following the file's existing style.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest -q` (timeout 600s)
Expected: PASS, whole suite.

- [ ] **Step 6: Commit**

```bash
git add config/projects.yaml docs/projects-yaml.md CHANGELOG.md
git commit -m "feat(config): declare roots for the projects the corpus identifies"
```

---

## Deployment note

Do **not** treat merge as deployment. This repo has now bitten twice on that: the broker resolves `releases/latest` and needs the `v*` tag, and the local CLI is installed non-editable into `.venv/site-packages` and needs

```bash
uv pip install --python .venv/bin/python --force-reinstall --no-deps .
```

After deploying, verify against the live broker rather than the test suite:

```bash
job submit --project some_new_label --cwd /home/mjarnold/jepagame --dry-run -- true
```

Expected: `validation.effective_project` is `jepagame`,
`validation.effective_priority` is 78, and `validation.effective_matched_root`
is `/home/mjarnold/jepagame`. Add `--json` if the CLI's dry-run output does not
show the raw validation block.
