# Plan: Per-project defaults file (`projects.yaml` enforcement)

Design notes for the per-project `defaults:` feature. Read alongside the
live source files (`src/jobd/app.py`, `src/jobd/config.py`,
`src/jobd/models.py`, `src/job_cli/cli.py`).

---

## 1. File location and format

### Location

`projects.yaml` already lives on the broker host at the path passed to
`build_app(projects_path=...)`. In production this resolves to
`/app/config/projects.yaml` (controlled by `JOBD_CONFIG_DIR` in `main.py:13`).
In the Docker stack this maps to `/srv/jobd/config/projects.yaml`
on the broker host, bind-mounted into the container.

There is no client-side copy. Per-project defaults are broker-side only.
Rationale: the broker is the single source of truth for job configuration; a
client-side copy would diverge between machines (laptop / desktop) and make
`--explain` unreliable without a second sync protocol.

The file does not move. Extend the existing schema in place.

### Schema

Extend each project entry with an optional `defaults:` block. All keys under
`defaults` are optional; absence means "fall through to global constant."

```yaml
# /app/config/projects.yaml

projects:
  project-c:
    priority: 55
    defaults:
      max_wall_s: 14400 # 4h; project-c GPU runs are long
      idle_timeout_s: 1800 # 30m; hang guard
      host_pin: desktop # always route to the GPU box
      requires:
        gpu: true
        needs: ["cuda"]
      preemptible: true
      priority: 55 # redundant with top-level but explicit
      escalate_to_arc: false

  project-a:
    priority: 55
    defaults:
      max_wall_s: 28800 # 8h; LoRA runs
      idle_timeout_s: 3600 # 1h
      host_pin: desktop
      requires:
        gpu: true
        needs: ["cuda"]
      preemptible: true
      escalate_to_arc: false

  project-b:
    priority: 80
    defaults:
      max_wall_s: 86400 # 24h; RNA-seq pipelines
      idle_timeout_s: 7200 # 2h
      host_pin: any
      requires:
        needs: ["R"]
      preemptible: false
      escalate_to_arc: true # future ARC routing; ignored until ARC backend ships

  _default:
    priority: 40
    # no defaults block: global constants apply
```

### Editing

Hand-edit the file directly. CLI read-back via `job projects list` (existing
command) is extended to show defaults when present. No `job projects
set-default` command in this iteration — hand-edit is sufficient and keeps
the CLI surface small. A `POST /reload` call or broker restart picks up
edits.

For programmatic updates (future scripts), `job reload` sends `POST /reload`
(existing endpoint, `app.py:526`).

### Versioning

The file is checked into the repo at `config/projects.yaml` and bind-mounted
into the Docker container. Changes go through git on the broker host. Same pattern as
`profiles.yaml` and `classifier.yaml`.

---

## 2. Schema — exhaustive key list

All keys live under `projects.<name>.defaults`. The block is optional per
project; absent keys fall through to the global constant.

| Key               | Type           | Global default                               | Semantic                                              | Overrides which flag/field                                  |
| ----------------- | -------------- | -------------------------------------------- | ----------------------------------------------------- | ----------------------------------------------------------- |
| `max_wall_s`      | `int \| null`  | `null` (no limit)                            | Kill job after N wall-clock seconds                   | `--max-wall` / `JobSubmit.max_wall_s`                       |
| `idle_timeout_s`  | `int \| null`  | `null` (no limit)                            | Kill job after N seconds with no output               | `--idle-timeout` / `JobSubmit.idle_timeout_s`               |
| `host_pin`        | `str \| null`  | `"any"`                                      | Pin to named worker                                   | `--host` / `JobSubmit.host_pin`                             |
| `requires`        | `dict \| null` | `null`                                       | Capability requirements (same shape as `JobRequires`) | `--needs`, `--gpu`, `--arch`, `--os` / `JobSubmit.requires` |
| `requires.arch`   | `str`          | `"any"`                                      | Worker arch constraint                                | sub-field                                                   |
| `requires.os`     | `str`          | `"any"`                                      | Worker OS constraint                                  | sub-field                                                   |
| `requires.gpu`    | `bool \| null` | `null`                                       | GPU requirement                                       | `--gpu/--no-gpu`                                            |
| `requires.needs`  | `list[str]`    | `[]`                                         | Capability tags                                       | `--needs`                                                   |
| `preemptible`     | `bool \| null` | `null` → treated as `false`                  | Whether broker may preempt this job                   | `--preemptible` / `JobSubmit.preemptible`                   |
| `priority`        | `int \| null`  | `null` → inherits `projects.<name>.priority` | Base priority (0–100)                                 | `--priority-delta` base; same as top-level `priority`       |
| `escalate_to_arc` | `bool`         | `false`                                      | ARC HPC routing eligibility                           | future `--host arc` routing                                 |

Schema layout rationale: flat under `defaults` (not nested per-feature) to
match the existing `profiles.yaml` `ProfileSpec` pattern and keep YAML
iteration simple in the loader. The `requires` sub-object is the only
nested key, for the same reason `JobRequires` is a separate model — it
groups the four capability selectors that are already treated as a unit
elsewhere.

`escalate_to_arc` is parsed and stored but the matcher does not act on it
until the ARC backend ships. Its presence in the schema now means no
migration is needed when ARC lands.

---

## 3. Resolution order

From highest to lowest precedence:

1. **Explicit CLI flag** — any flag the user passes explicitly overrides
   everything.
2. **Project default** — from `projects.yaml: <project>.defaults.*`
3. **Profile default** — from `profiles.yaml` (already implemented; sits
   between project defaults and global constants).
4. **Global constant** — broker-level hardcoded fallback.

For `host_pin` specifically, the existing profile `host_hint` (applied at
`app.py:130-131`) is treated as equivalent to the project default in
precedence. When both are present, project default wins over profile hint.

### Walk-through example

Command: `job submit --project project-c -- ./run.sh`

With `project-c.defaults` set to `max_wall_s: 14400`, `idle_timeout_s: 1800`,
`host_pin: desktop`, `requires: {gpu: true, needs: [cuda]}`,
`preemptible: true`:

| Field            | CLI value        | Project default | Profile default | Global  | **Effective** | Source          |
| ---------------- | ---------------- | --------------- | --------------- | ------- | ------------- | --------------- |
| `max_wall_s`     | `null`           | `14400`         | —               | `null`  | **14400**     | project default |
| `idle_timeout_s` | `null`           | `1800`          | —               | `null`  | **1800**      | project default |
| `host_pin`       | `"any"`          | `"desktop"`     | —               | `"any"` | **"desktop"** | project default |
| `requires.gpu`   | `null`           | `true`          | —               | `null`  | **true**      | project default |
| `requires.needs` | `[]`             | `["cuda"]`      | —               | `[]`    | **["cuda"]**  | project default |
| `preemptible`    | `null`           | `true`          | —               | `false` | **true**      | project default |
| `priority`       | base=55, delta=0 | —               | —               | —       | **55**        | existing logic  |

Effective `JobSubmit` body the CLI sends (before broker resolution):

```json
{
  "cmd": ["./run.sh"],
  "cwd": "/current/working/dir",
  "project": "project-c",
  "host_pin": "any",
  "priority_delta": 0,
  "requires": null,
  "max_wall_s": null,
  "idle_timeout_s": null
}
```

After broker applies project defaults, the Job row is written with:

```json
{
  "host_pin": "desktop",
  "preemptible": true,
  "requires": {
    "gpu": true,
    "needs": ["cuda"],
    "arch": "any",
    "os": "any",
    "idempotent": false
  },
  "max_wall_s": 14400,
  "idle_timeout_s": 1800,
  "priority": 55
}
```

---

## 4. Where in the codebase resolution happens

### Decision: broker-side resolution

Resolve in `app.py:submit` (the `POST /submit` handler), immediately after
the existing `resolve_priority` call at line 128 and before the `host_pin`
override block at lines 129-131.

Rationale: the broker already resolves priority (`resolve_priority`,
line 128), profile (`resolve_profile`, line 124), and host_hint
(lines 130-131) from its own loaded state. Adding project defaults here is
consistent, ensures all clients (CLI, MCP, future REST callers) see the same
defaults, and means the single `POST /reload` endpoint refreshes all of them
together.

CLI-side resolution is insufficient on its own: the MCP server (`server.py`)
submits directly via the broker HTTP client and has no access to a local
`projects.yaml`. Duplicating resolution logic in both places creates drift.

### New data structures: `ProjectEntry` and `ProjectDefaults`

In `config.py`, replace the current `dict[str, int]` loaded by
`load_projects` with a richer structure:

```python
# src/jobd/config.py

@dataclass
class ProjectDefaults:
    max_wall_s: int | None = None
    idle_timeout_s: int | None = None
    host_pin: str | None = None          # None means "do not override"
    requires: JobRequires | None = None
    preemptible: bool | None = None      # None means "do not override"
    priority: int | None = None
    escalate_to_arc: bool = False

@dataclass
class ProjectEntry:
    priority: int
    defaults: ProjectDefaults = field(default_factory=ProjectDefaults)
```

`load_projects` returns `dict[str, ProjectEntry]`. Existing callers that
expect `dict[str, int]` must be updated atomically in the same commit:

- `resolve_priority` at `config.py:72` — extract `.priority` from each entry.
- `app.py:128` — calls `resolve_priority`; no change once the function
  signature is updated.
- `app.py:516-522` (`list_projects`, `set_project_priority`,
  `nudge_project_priority`) — read and mutate `state["projects"]`. Update to
  work with `dict[str, ProjectEntry]`.
- `_persist_projects` at `app.py:648-653` — serialize both `priority` and
  the `defaults` block, preserving any keys that were in the original YAML.
  **This is the highest-priority correctness risk** (see §8).

### New function: `resolve_project_defaults`

Add to `config.py`:

```python
def resolve_project_defaults(
    projects: dict[str, ProjectEntry], project_name: str
) -> ProjectDefaults:
    """The project's own defaults, layered OVER `_default`'s."""
    floor = projects.get("_default")
    entry = projects.get(project_name)
    if entry is None:
        return floor.defaults if floor is not None else ProjectDefaults()
    if floor is None or floor is entry:
        return entry.defaults
    return _merge_defaults(floor.defaults, entry.defaults)
```

**`_default.defaults` is a FLOOR, not a fallback.** Every project inherits it, and
overrides it one key at a time — setting `idle_timeout_s` does not drop
`max_wall_s`. Every field of `ProjectDefaults` therefore uses `None` as its unset
sentinel (including the bools), because a merge cannot otherwise distinguish "the
project said nothing" from "the project said `false`".

This was `projects.get(name) or projects.get("_default")` — an either/or — until
2026-07-14. A project *with* an entry never saw `_default.defaults`, so the block
`config/projects.yaml` calls "the FLEET-WIDE hang-guard" (`idle_timeout_s`,
`max_wall_s` — the zombie reaper) reached only projects that were **not** configured.
Registering 32 projects to give them priorities disarmed the hang-guard on all 32.
Guarded by `tests/test_project_defaults_floor.py`.

### Changes to `app.py:submit` (lines 121-203)

Insert a block after line 128 (after `priority = resolve_priority(...)`) and
before line 129:

```python
proj_defaults = resolve_project_defaults(state["projects"], req.project)
```

Then update each field resolution to check project defaults in the chain:
CLI value → project default → profile default → global constant.

**`max_wall_s`** (currently at line 187):

```python
max_wall_s = req.max_wall_s
if max_wall_s is None:
    max_wall_s = proj_defaults.max_wall_s
```

**`idle_timeout_s`** (currently at line 188): same shape.

**`host_pin`** (currently lines 129-131):

```python
host_pin = req.host_pin
if host_pin == "any" and proj_defaults.host_pin:
    host_pin = proj_defaults.host_pin
elif host_pin == "any" and profile_spec and profile_spec.host_hint:
    host_pin = profile_spec.host_hint
```

(Project default takes precedence over profile hint.)

**`preemptible`** (currently line 136):

`JobSubmit.preemptible` is `bool = False`, not `bool | None`, so the CLI
always sends `false` when the flag is absent. We cannot distinguish "user
passed `--preemptible false` explicitly" from "user did not pass the flag at
all." Change `JobSubmit.preemptible` to `bool | None = None` and update the
CLI to omit the field from the body when not explicitly passed:

```python
# cli.py: only include preemptible in body if flag was explicitly passed
if preemptible is not None:
    body["preemptible"] = preemptible
```

```python
# app.py: resolution
preemptible = req.preemptible
if preemptible is None:
    preemptible = proj_defaults.preemptible
if preemptible is None and profile_spec:
    preemptible = profile_spec.preemptible
if preemptible is None:
    preemptible = False
```

**`requires`** (currently lines 142-144):

```python
requires = req.requires
if requires is None:
    requires = proj_defaults.requires
if requires is None and profile_spec and profile_spec.requires:
    requires = profile_spec.requires
```

### File reload

`POST /reload` at `app.py:526-530` already calls `load_projects`,
`load_profiles`, and `load_classifier_rules` and stores results in `state`.
No change needed — the updated `load_projects` returning
`dict[str, ProjectEntry]` is picked up automatically.

The broker does NOT watch file mtime. Reload requires `job reload` (new
one-line CLI command calling `POST /reload`) or broker restart. Both are
acceptable; mtime-watching is not worth the complexity for a config that
changes rarely.

---

## 5. `--explain` / dry-run surface

### New endpoint: `POST /resolve`

```python
@app.post("/resolve")
def resolve_job(req: JobSubmit) -> dict:
    """Dry-run submit: return effective resolved config without enqueuing.

    Performs all the same resolution steps as POST /submit (priority,
    profile, project defaults, host_pin, preemptible, requires, max_wall_s,
    idle_timeout_s) but does not write a Job row. Returns the effective
    values and the source of each (cli|project_default|profile|global).
    """
```

Returns a `ResolvedConfig` model (new, in `models.py`):

```python
class FieldResolution(BaseModel):
    value: Any
    source: Literal["cli", "project_default", "profile", "global"]

class ResolvedConfig(BaseModel):
    project: str
    effective_priority: FieldResolution
    effective_host_pin: FieldResolution
    effective_max_wall_s: FieldResolution
    effective_idle_timeout_s: FieldResolution
    effective_preemptible: FieldResolution
    effective_requires: FieldResolution
    effective_escalate_to_arc: FieldResolution
    submit_warning: str | None
```

### `--explain` flag in CLI

```python
explain: bool = typer.Option(False, "--explain", help="print resolved config without submitting")
```

When `--explain` is set, the CLI POSTs `/resolve` instead of `/submit` and
prints the result. No job is created.

Output (human-readable; parseable with `jq` if `--json`):

```
resolved config for project project-c:
  priority:         55              [source: project config]
  host_pin:         desktop         [source: project default]
  max_wall_s:       14400 (4h0m0s)  [source: project default]
  idle_timeout_s:   1800 (0h30m0s)  [source: project default]
  preemptible:      true            [source: project default]
  requires:         gpu=true needs=[cuda]  [source: project default]
  escalate_to_arc:  false           [source: project default]
  submit_warning:   none
```

`--explain` round-trips through the broker so it sees the live
`projects.yaml` state. This is correct: the broker already has resolution
logic, and `--explain` is a transparency tool for the broker's decisions,
not a local preview.

---

## 6. Migration / backward compat

### In-flight jobs

No change. Job rows already store `max_wall_s`, `idle_timeout_s`,
`host_pin`, `preemptible`, `requires_json` at submit time. In-flight jobs
were submitted before project defaults existed and retain their original
values.

### Existing CLI scripts that pass flags explicitly

Continue to work without change. CLI flags (`--max-wall`, `--idle-timeout`,
`--host`, `--preemptible`, `--needs`, `--gpu`) all continue to be honored.
When a flag is supplied, the corresponding `req` field is non-null and the
project default is skipped.

Exception: `--preemptible` requires the sentinel change. `JobSubmit.
preemptible` becomes `bool | None`. Clients that omit the field from the
JSON body get `None` and fall through to project defaults. Clients that
explicitly send `"preemptible": false` (including old CLI versions)
continue to get `false` — no behavior change for them.

### Missing `projects.yaml`

Update `load_projects` to catch `FileNotFoundError`:

```python
try:
    raw = Path(path).read_text()
except FileNotFoundError:
    log.info("no projects.yaml found at %s; using global defaults", path)
    return {"_default": ProjectEntry(priority=40)}
```

### Unknown project name in submit

Do NOT refuse. Submit succeeds with global defaults. Broker emits a
`submit_warning` on the returned `JobInfo`:

```
project 'my-new-project' has no entry in projects.yaml; using global defaults
```

Refusing would break first-time `job submit --project new-experiment`. The
warning is surfaced via the existing `warning` field on `JobInfo`,
concatenated with any other submit-time warnings using the existing
`"; ".join(warnings)` pattern at `app.py:196-198`.

---

## 7. Test plan

### `tests/test_projects_yaml.py`

1. `test_load_projects_full_schema` — YAML with all keys under `defaults`;
   assert all `ProjectDefaults` fields parse correctly.
2. `test_load_projects_missing_defaults_block` — entry with only
   `priority`; assert `defaults` is a zero-valued `ProjectDefaults`.
3. `test_load_projects_missing_file` — `FileNotFoundError` returns
   `{"_default": ProjectEntry(priority=40)}` without raising.
4. `test_load_projects_malformed_yaml` — corrupt YAML raises
   `yaml.YAMLError`, not silent empty dict.
5. `test_load_projects_unknown_keys_in_defaults` — unrecognized key under
   `defaults` is silently dropped (lenient for future additions).
6. `test_persist_projects_round_trip` — write a `ProjectEntry` with
   defaults, call `_persist_projects`, reload, assert defaults survive
   intact. **This is the critical regression test for the
   `_persist_projects` bug.**

### `tests/test_resolution_order.py`

1. `test_cli_flag_wins_over_project_default` — `req.max_wall_s=7200`,
   project default `14400`; effective `7200`.
2. `test_project_default_wins_over_global` — `req.max_wall_s=None`, project
   default `14400`, global `None`; effective `14400`.
3. `test_missing_project_falls_through_to_global` — unknown project; assert
   global defaults applied; assert `job.warning` contains "no entry in
   projects.yaml".
4. `test_host_pin_project_default_overrides_sentinel` —
   `req.host_pin="any"`, project default `"desktop"`; Job row
   `host_pin="desktop"`.
5. `test_host_pin_explicit_cli_not_overridden` — `req.host_pin="laptop"`,
   project default `"desktop"`; Job row `host_pin="laptop"`.
6. `test_preemptible_sentinel_detects_cli_absence` — `req.preemptible=None`,
   project default `True`; Job row `preemptible=True`.
7. `test_preemptible_false_explicit_not_overridden` —
   `req.preemptible=False`, project default `True`; Job row
   `preemptible=False`.
8. `test_requires_project_default_applied` — `req.requires=None`, project
   default `{gpu:true, needs:["cuda"]}`; `requires_json` reflects project
   default.
9. `test_project_default_wins_over_profile_for_host_pin` — project default
   `"laptop"`, profile `host_hint="desktop"`; effective `"laptop"`.

### `tests/test_explain.py`

1. `test_explain_returns_resolved_body_without_job` — `POST /resolve`
   returns expected sources; no Job row created.
2. `test_explain_cli_flag_wins` — flag overrides project default; source
   `"cli"`.
3. `test_explain_global_fallback` — unknown project; all sources
   `"global"`; submit_warning surfaces.
4. `test_explain_output_format` — `job submit --project project-c --explain`
   stdout contains expected header and source annotations.

### Integration test

`tests/integration/test_project_defaults_live.py`:

1. `test_project_defaults_applied_to_db_row` — fixture YAML with
   `project-c.defaults.max_wall_s=9999` and `host_pin="laptop"`; submit;
   query DB row; assert resolved values present.

Mark with `@pytest.mark.live` and guard with `RUN_LIVE_JOBD=1`.

---

## 8. Effort estimate and dependencies

### Effort estimate

| Task                                                                   | Hours         |
| ---------------------------------------------------------------------- | ------------- |
| `ProjectEntry`/`ProjectDefaults` dataclasses + `load_projects` rewrite | 1.5           |
| Update `_persist_projects` for round-trip safety                       | 0.5           |
| Update `resolve_priority` signature for new type                       | 0.5           |
| Project defaults resolution block in `app.py:submit`                   | 1.0           |
| `JobSubmit.preemptible` sentinel change + CLI update                   | 0.5           |
| `POST /resolve` endpoint + `ResolvedConfig` model                      | 1.5           |
| `--explain` flag in CLI + output formatter                             | 1.0           |
| Update `projects_list` endpoint to include defaults                    | 0.5           |
| Missing-project warning wiring                                         | 0.5           |
| Tests (all four test files)                                            | 3.0           |
| Update `conftest.py` fixture `sample_projects_yaml`                    | 0.25          |
| **Total**                                                              | **~11 hours** |

### Prerequisites — must change atomically in same commit

1. **`load_projects` return type change** from `dict[str, int]` to
   `dict[str, ProjectEntry]`. Six call sites must be updated atomically:
   `config.py:resolve_priority` (line 72), `app.py:list_projects`
   (505-507), `app.py:set_project_priority` (509-513),
   `app.py:nudge_project_priority` (515-522), `app.py:_persist_projects`
   (648-653), `conftest.py:sample_projects_yaml`.

2. **`JobSubmit.preemptible` sentinel change**: `bool` → `bool | None =
None`. CLI must omit the field when the flag is not explicitly passed.

3. **No new DB columns needed.** All target fields exist in `jobs` already.

4. **No new `JobSubmit` fields needed.** Only the `preemptible` type change.

5. **Matcher requires no changes.** Project defaults are resolved at submit
   time and stored on the Job row; the matcher operates on the resolved
   row.

### API contract changes

- `JobSubmit.preemptible: bool → bool | None`. Backward-compatible: explicit
  `true`/`false` continues to work; omitting the field now falls through to
  project defaults instead of hardcoded `false`.
- New endpoint `POST /resolve` returning `ResolvedConfig`. Additive only.
- `POST /submit` response unchanged: resolved values appear in the existing
  `JobInfo` fields; some previously-`null` fields may now be non-null.

---

## 9. Out of scope (explicit non-goals)

- Config management framework: no env-var interpolation, no secrets, no
  per-host overrides, no inheritance chains. One flat YAML file.
- ARC routing logic: `escalate_to_arc` is parsed and stored. The matcher
  does not act on it. Separate BACKLOG item.
- Auto-preempt opt-in defaults: `preemptible: true` here means "jobs in
  this project are preemptible by default." The auto-preempt-by-default
  policy migration is a separate BACKLOG item (#8 in work order).
- `job projects set-default` CLI mutation commands: hand-edit + `job
reload` is sufficient.
- Per-host project override: no per-machine copy.
- Priority duplication: `priority` under `defaults` is accepted in the
  schema (symmetry with other fields) but the loader uses the top-level
  `priority` key as the canonical source; `defaults.priority` only applies
  when the top-level is absent.
