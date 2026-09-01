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
