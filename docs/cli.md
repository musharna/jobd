# CLI notes: arrays, adoption, dry runs, coming from pueue

The command summary is in the [README](../README.md#cli); `job <command> --help` has every flag.

## Submit

```
job submit -p PROJ [--gpu] [--vram-required N] [--needs TAG]... [--count N | --sweep K=v1,v2]... [--wait] -- CMD...
```

## Dry-run a submit

`job submit --explain` dry-runs the resolution (priority, profile, project defaults, host pin) and prints the effective config without enqueuing anything.

## Adopt a process you already started

`job adopt` makes a process started outside jobd (`nohup`, tmux) visible to the broker: it holds a slot and its VRAM until it exits, and nothing is launched. Its exit code is unknowable, so it ends `orphaned`, never `completed` — see [adoption.md](adoption.md).

## Job arrays

Submit N jobs from one template with `--count N`. Each member is a normal job — it routes, runs, preempts, and checkpoints independently — and `{i}` in the command is replaced by the member's 0-based index:

```bash
job submit -p train --count 8 -- python train.py --fold {i}
# → Submitted array A42: 8 jobs (ids 42..49)

job list --array A42         # the members, with their index annotations
job status A42               # aggregate: state tally + per-member rollup
```

The array is identified as `A<id>` (the first member's job id). `job status A42` exits non-zero if any member ended in a non-completed terminal state, so it composes with shell `&&`.

For a grid search, use `--sweep KEY=v1,v2,v3` (repeatable) instead of `--count`. The broker fans out the cartesian product of all axes, substituting `{KEY}` per member; `{i}` (the flat member index) is also available:

```bash
job submit -p train --sweep lr=0.1,0.01 --sweep seed=1,2,3 \
  -- python train.py --lr {lr} --seed {seed} --out run-{i}
# → Submitted array A50: 6 jobs (ids 50..55)   # 2 × 3 = 6 members
```

`--sweep` and `--count` are mutually exclusive, the product is capped at 1000 members, and `i` is reserved as an axis key. Substitution is a literal `{key}` replace (not `str.format`), so JSON literals and shell braces in the command pass through untouched.

## Coming from pueue or task-spooler

Most verbs map directly; the queue just spans every machine you own.

| You ran…                          | With jobd                                                                  |
| --------------------------------- | -------------------------------------------------------------------------- |
| `tsp <cmd>` / `pueue add -- <cmd>`| `job submit -p <project> -- <cmd>`                                          |
| `tsp -w` / `pueue follow <id>`    | `job logs -f <id>` (or `job wait <id>`) — streams, exits with the job's own exit code |
| `tsp` / `pueue status`            | `job list`                                                                  |
| `pueue log <id>`                  | `job logs <id>`                                                             |
| commands piped to `simple_gpu_scheduler` | `... \| job submit -p <project> --stdin` — one job per line, fleet-wide |
| `pueue kill <id>`                 | `job cancel <id>`                                                           |
| `pueue group` / parallelism limits| approximately: projects + priorities (`projects.yaml`); per-worker slots via `JOBD_WORKER_MAX_CONCURRENT_JOBS` |

What you lose: no pause/resume, stash or editing of a queued job (cancel and resubmit instead), no named groups with their own parallelism limit, and if a worker dies mid-job, a running job not marked idempotent ends `orphaned` rather than being re-run. A broker and one worker on the same host otherwise behave like a network-reachable pueue.
