- **The cwd-derived project identity feature is no longer inert: `agrigen`, `jepagame`,
  `orchid-sdxl`, and `dreamer-chassis` now declare `roots:` in `config/projects.yaml`.**
  The matching machinery and its acceptance gate against 3,608 rows of real job history
  (`tests/test_corpus_replay.py`) landed first, but no project had ever declared a root,
  so a job typed with an unregistered run label (`pillar2a1_sweep`, `arf-promoter`,
  `orchid-sdxl-stage4b`, ...) still fell through to `_default` regardless. Each root was
  picked by replaying `tests/data/project_cwd_corpus.csv` grouped by cwd and keeping only
  directories where every project name ever typed from them reads as a variant of one
  project — deliberately excluding `/home/mjarnold/trellis2` and `.../hunyuan3d`
  (>=97% agrigen-typed in the corpus, trellis2 the single largest cwd in it) because both
  are generic third-party tool checkouts a differently-owned future clone could silently
  inherit. `docs/projects-yaml.md` gains a `roots:` section covering the schema, the
  three-rule resolution order (an explicit, registered `--project` always wins over cwd —
  the safety property the other two rules sit behind), longest-root-wins, component-wise
  (non-symlink-resolving) path matching, and the no-identity-plus-warning outcome when two
  projects' roots both claim a cwd. `--project` remains required; a cwd-derived identity
  changes what its value _means_, not whether it must be given.

  Shipping in the same release, the user-visible surface that makes a substituted identity
  findable and explainable rather than merely correct:
  - `job list --project NAME` now matches **either** the scheduling identity or the typed
    run label. Without this a job submitted as `pillar2a1_sweep` and priced as `jepagame`
    was unfindable under the only name its submitter ever knew it by.
  - `JobInfo` gains `project_label`: the name as typed, `null` when it agrees with
    `project`, so the field reads as "something was substituted here".
  - `POST /resolve` gains `project_label` and `matched_root` (the root that supplied the
    identity, `null` when cwd was not consulted), and `job submit --explain` renders both —
    the dry run is where an operator asks "why did this get that priority?", and the root
    is the answer.
  - A new `cwd_identity_applied` event is recorded per job whenever a cwd-derived identity
    is applied, carrying the project, the typed label, and the matched root. It is not a
    warning: nothing went wrong, and a substitution that is invisible in the event stream
    is one nobody can audit after the fact.
  - The `job submit` substitution note goes to **stderr** (stdout carries the submit JSON
    alone, so `job submit ... | jq .id` parses). It reports what it can observe — that the
    typed name and the scheduling one fold together, or that they do not and cwd supplied
    the identity — rather than naming a mechanism it cannot verify: when two registered
    projects fold onto the same key the fold is refused and cwd decides, yet the names
    still fold together. `job submit --explain` carries `matched_root` and answers it.
  - Path matching now collapses `..` lexically before comparing components. It previously
    did not, so `/home/mjarnold/jepagame/../../tmp` — a job actually running in `/tmp` —
    matched a root of `/home/mjarnold/jepagame` and was priced at that project's `78`.
    `cwd` is free text on the wire, so this was caller-reachable. A root containing a `..`
    component is now a load error, in the same raise-don't-drop style as the other root
    validations. Symlinks and bind mounts remain unresolved by design: the broker cannot
    see the worker's filesystem.
