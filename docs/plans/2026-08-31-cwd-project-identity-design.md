# cwd-derived project identity

Design, 2026-08-31. **Status: shipped in v0.5.42 (2026-09-01).** Historical design
record; the shipped behaviour diverged in places (lexical `..` collapse, the
stderr substitution note's wording, `matched_root` in the dry-run plan). The
current specification is `docs/projects-yaml.md` §10.

## The problem

`--project` is required at every submit and is free text. It does two unrelated
jobs at once:

1. **Scheduling identity** — which row of `projects.yaml` prices this job, and
   supplies its defaults.
2. **Run label** — how a human finds this batch again in `job list`.

Those two wants pull in opposite directions. Job 1 wants a small, stable,
registered vocabulary. Job 2 wants a fresh string per experiment. Sharing one
field means every submitter has to satisfy 1 while thinking about 2, and the
failure is silent: an unregistered name falls through to `_default` and runs at
40 with no error.

v0.5.39–41 closed the _spelling_ half of this (`epsilon` now folds onto
`epsilon`; a write reports the name it landed on). The retyped-string
mechanism itself is untouched, and it is the larger half.

### Measured, live broker DB, 2026-08-31

3,608 job rows; 249 distinct `(project, cwd)` pairs; 138 distinct project
names against 148 distinct cwds.

|                                                  | count   |
| ------------------------------------------------ | ------- |
| jobs whose project name is not registered        | 470     |
| ... from a specific (non-scratch) cwd            | 376     |
| ... whose cwd already hosts a registered project | **241** |

That last row is the addressable population: 241 jobs that a human would say
belong to a registered project, priced as though they belonged to nothing.

The top entries show the mechanism plainly — these are run labels, not
projects:

```
 27  pillar2a1_sweep           /home/user/beta        (beta is registered at 78)
 26  gamma-geo           /home/user/gamma
 18  delta           /home/user/delta
 16  pillar4_1                 /home/user/beta
 15  gamma-stage2             /home/user/gamma
```

The relation is genuinely many-to-many — 33 projects span more than one cwd, 26
cwds span more than one project — so cwd cannot simply _replace_ the field.
It can only supply the identity when the typed name does not.

## Design

Split the two jobs. Keep one field's meaning fixed and add the other beside it.

- **Scheduling identity** stays in `Job.project` and keeps deciding priority and
  defaults. It is now _derivable_, not merely typed.
- **Run label** is what the submitter typed. Recorded, displayed, filterable.

### Resolution order

At `/submit` and `/resolve`, first match wins:

1. **The typed name is a registered project** (after `canonical_project_name`
   folding). It is the identity. Unchanged behaviour.
2. **Else the cwd matches a project's `roots:`.** That project is the identity.
3. **Else `_default`,** and the existing `unknown_project` warning still fires.

Rule 1 first is what makes this safe: no submit that resolves correctly today
can have its priority changed by this feature. cwd fills in an identity only
where there is currently none.

### `roots:` in projects.yaml

`ProjectEntry` gains `roots: list[str]` beside `priority` and `defaults`
(`src/jobd/config.py:57`). Top level, not inside `defaults:` — `defaults:` holds
per-job field defaults and a root is not one of those.

```yaml
projects:
  beta:
    priority: 78
    roots:
      - /home/user/beta
  gamma:
    priority: 60
    roots:
      - /home/user/gamma
```

Matching is **longest root wins**, compared **path-component-wise, not as a
string prefix**. `/home/user/beta2` must not match root
`/home/user/beta`; a naive `str.startswith` says it does. This is the
single most likely defect in the feature and gets a dedicated negative test.

Symlinks are not resolved. The broker has no access to the worker's filesystem,
so it compares the path it was handed. This is stated so that a future reader
does not "fix" it into a `realpath` call the broker cannot make.

Two projects declaring roots that both match yields **no** identity, plus a
load-time warning naming them. This mirrors `canonical_project_name`'s existing
posture on an ambiguous fold (`config.py:355`): say so and route as before,
rather than pick one. Guessing here runs someone's jobs at a neighbour's
priority — the exact failure the feature exists to prevent, inverted.

A root that is not an absolute string is a **load error**, raised, not skipped.
`_DEFAULTS_KEYS` silently drops unknown keys inside `defaults:` by design, and a
silently dropped root would present to the operator as "the feature just does
not work on my machine".

### Schema

`Job.project` already stores the **resolved** name, not the typed one —
`submit.py:84` sets `project = eff.project` and `:251` writes it. So the typed
label is discarded today, and the new column carries the label:

```
("project_label", "VARCHAR(100)")   # appended to _JOB_ADDS, db.py:184
```

NULL means "the same as `project`". No backfill, every historical row stays
valid, and `migrate()` (`db.py:222`) is already the additive, idempotent path
for exactly this. There is no alembic in this repo and this design does not add
one.

Assigning the columns the other way — `Job.project` holding the label — was
considered and rejected. It would silently redefine an indexed column that
`job list --project`, the matcher, and the priority join all already read, and
change the meaning of 3,608 existing rows.

### Observability

A new event, `cwd_identity_applied`, fired when rule 2 supplies the identity,
carrying the typed label, the matched root, and the resolved project.

It **must** be added to `KNOWN_EVENTS` (`src/jobd/models.py:406`) in the same
commit. An unlisted name still reaches `events.jsonl`, but
`broker/events.py:74` collapses it into the `other` bucket, so an alert written
against it would never fire and would look healthy doing so. That is finding
#18 of this same audit and the comment block already in `KNOWN_EVENTS` says so;
this design does not re-create it.

`unknown_project` keeps firing for rule 3, unchanged, so the existing "fell
through to `_default`" signal is not weakened by the new one.

### Surfaces

- `/resolve` reports the resolved identity and, when rule 2 fired, the root that
  matched — the read path already names the resolved project (`routes/config.py:142`).
- `job submit` echoes the fold when it happens, on the same principle as
  `_echo_project_write`: a silent substitution leaves the operator believing
  their typed name is registered.
- `job list --project` matches against **either** column, so an existing filter
  on a label keeps working and a filter on the identity now finds the whole
  project's work.

## Testing

Unit: precedence (all three rules), longest-root selection, the
`beta2`/`beta` boundary negative, ambiguous-roots warn-and-fall-through,
label preserved in every branch, bad-root load error.

The real-execution check is a **corpus replay**: run the resolver over every one
of the 3,608 historical job rows from the live DB — 249 distinct
`(project, cwd)` pairs — and assert both directions —

- the ~241 recoverable jobs now resolve to their root's project, and
- **every job whose typed name is already registered keeps its exact current
  priority.**

The second assertion is the regression control and the one that matters. A
synthetic fixture here would encode my beliefs about the corpus rather than the
corpus, which is precisely how the v0.5.40 CLI bug shipped: every CLI test fed
the command a hand-written response, so no test could disagree with its author.

Each new test is run against the pre-change code first and must fail for the
stated reason. The corpus replay needs a positive control in the same test — the
"registered names unchanged" arm — or a resolver that returns nothing at all
would read as a clean pass.

## Files

| File                        | Change                                                                      |
| --------------------------- | --------------------------------------------------------------------------- |
| `src/jobd/config.py`        | `roots` on `ProjectEntry`, loader + validation, resolver, ambiguity warning |
| `src/jobd/models.py`        | `cwd_identity_applied` in `KNOWN_EVENTS`; `/resolve` response field         |
| `src/jobd/broker/submit.py` | wire resolution in, emit the event, store the label                         |
| `src/jobd/db.py`            | `project_label` column + `_JOB_ADDS` entry                                  |
| `src/job_cli/cli.py`        | echo the fold; `--project` filter matches either column                     |
| `docs/projects-yaml.md`     | document `roots:` and the resolution order                                  |
| `config/projects.yaml`      | roots for the projects the corpus shows need them                           |

## Out of scope

Eighteen entries currently sit at priority 65, and most of them are
experiment-arm names rather than projects (`h_grammar`,
`c_mimicry`, `og_control`, `eff_host`, `k2d`,
`arf-dimer-g1`, …) registered one at a time as a workaround for exactly this
gap. They become redundant once roots exist, but rule 1 keeps them winning and
nothing breaks while they stay. Retiring them is separate cleanup, after this
has run long enough to trust — and deciding which of the eighteen are arms and
which are real projects is part of that cleanup, not this design.

Also out of scope: making `--project` optional. It stays required; this design
changes what the value _means_, not whether it must be given.
