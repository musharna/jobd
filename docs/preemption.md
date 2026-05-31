# jobd preemption protocol (path B)

`jobd` can preempt a running job to free its slot for a higher-priority
queued job. By default the broker SIGTERMs the workload and reports the
final state as `preempted` — the workload dies and the project reruns it
later. Long-running training jobs can opt into a **checkpoint protocol**
that lets them save state durably during the SIGTERM grace window before
exiting.

## When does a job get preempted?

The sweeper auto-preempts a running job when:

1. Some other queued job has been queued for more than 5 minutes,
2. The running job is `preemptible: true` (per `--preemptible` or per-
   project default in `projects.yaml`),
3. Its priority is strictly less than the queued job's, and
4. It has been running for more than 5 minutes (the `AUTO_PREEMPT_MIN_RUNTIME_SECONDS` floor).

An operator can also force preemption manually:

```bash
job preempt <id>                 # request: this specific running job
job preempt-blockers <queued_id> # find a blocker for this queued job
```

## What the worker does

1. Broker sets `signal=preempt` on the running job.
2. Worker poll picks it up and SIGTERMs the workload (via the systemd
   user-scope cgroup).
3. Worker waits up to `checkpoint_grace_s` (default 60s, max 300s) for
   the workload to exit cleanly. After that the worker SIGKILLs.
4. If the worker observes the literal string `jobd-checkpoint-complete`
   in the job's stdout during that window, it POSTs
   `/jobs/{id}/checkpoint-complete` to the broker, which appends a
   `checkpoint_complete` event to `events.jsonl`. This is pure
   observability — the actual final state still flows through
   `/complete` once the workload exits.
5. Final state is `preempted`. Children with `depends_on` cascade-cancel
   (the input never materialized) unless `depends_on_any_exit=True`.

## What the workload does

The convention is: install a SIGTERM handler that durably checkpoints,
then prints `jobd-checkpoint-complete` and exits 0. `jobd.client`
provides a helper:

```python
from jobd.client import install_preemption_handler

def checkpoint(time_remaining: float) -> None:
    """Save model + optimizer state somewhere durable.
    `time_remaining` is seconds until SIGKILL — you can use it to
    self-cap a slow checkpoint."""
    save_checkpoint(model, optimizer, path=ckpt_path)

install_preemption_handler(checkpoint)
# ... start training ...
```

The helper:

- Reads `JOBD_CHECKPOINT_GRACE_S` from env (the worker sets this).
- Installs a SIGTERM handler that calls your function with the
  remaining seconds.
- On success: prints the `jobd-checkpoint-complete` sentinel and exits 0.
- On exception: prints the error to stderr and exits 1 — the sentinel
  is **not** printed, so the broker won't fire the
  `checkpoint_complete` event.

`jobd.client.time_remaining()` returns the live remaining seconds and
is also callable from anywhere inside the handler chain if you need it.

## Configuring the grace window

Per-job (CLI):

```bash
job submit --project p --cwd $(pwd) --preemptible \
  --checkpoint-grace 300 -- python train.py
```

Per-project default in `projects.yaml`:

```yaml
projects:
  long-training:
    priority: 60
    defaults:
      preemptible: true
      checkpoint_grace_s: 300
```

Resolution order: CLI flag > project default > worker default (60s).
Server caps at 300s (the sweeper interval) so a stuck checkpoint can't
pin a slot indefinitely.

## Observability

Each fire writes a JSON line to `<broker logs_dir>/events.jsonl`:

- `auto_preempt` — broker signaled SIGTERM (sweeper or manual).
- `checkpoint_complete` — worker saw the sentinel before the grace
  expired.

```bash
grep '"checkpoint_complete"' ~/jobd/logs/events.jsonl | jq
```

A successful path-B preemption looks like one `auto_preempt` followed
by a matching `checkpoint_complete` event for the same `job` /
`candidate_job` ID.

## Checkpoint directory contract

The worker creates a per-job directory and exposes its path to the workload via the `JOBD_CHECKPOINT_DIR` env var:

```
JOBD_CHECKPOINT_DIR=<root>/<job_id>/
```

`<root>` resolves as:

1. `$JOBD_WORKER_CHECKPOINT_ROOT` if set
2. `$XDG_DATA_HOME/jobd/checkpoints` if `XDG_DATA_HOME` is set and non-empty
3. `~/.local/share/jobd/checkpoints`

The override is treated as the literal checkpoint root — the worker appends `<job_id>/` directly under it, NOT `jobd/checkpoints/<job_id>/`. Tilde paths are expanded via `os.path.expanduser` (so `~/ckpts` works), but the override is otherwise used verbatim; supply an absolute path in systemd `Environment=` lines to avoid surprises.

The directory is `mkdir -p`-ed with mode `0700` before the workload starts. Convention is for the workload to write its durable checkpoint files inside that directory during the SIGTERM grace window. The resuming run reads from `$RESUME_FROM` (which the operator points at the prior run's `latest.<ext>` — see closing paragraph), NOT from `$JOBD_CHECKPOINT_DIR`, since the resuming job gets a fresh empty checkpoint dir of its own.

Mode `0700` and worker-user ownership mean cross-user resume on a shared filesystem doesn't work without re-permissioning. If `JOBD_WORKER_CHECKPOINT_ROOT` points at network storage so a _different_ user-or-host can resume, the operator is responsible for chmod/chown after the producing run finishes.

The broker does **not** sweep, rotate, or otherwise manage the contents. Cleanup of stale checkpoint directories is currently a manual / out-of-band concern. (Filed as a future backlog entry.)

Example workload (full path-B helper + checkpoint dir):

```python
import os
from pathlib import Path

import torch

from jobd.client import install_preemption_handler

ckpt_dir = Path(os.environ["JOBD_CHECKPOINT_DIR"])

def checkpoint(time_remaining: float) -> None:
    torch.save(
        {"model": model.state_dict(), "optimizer": opt.state_dict(), "step": step},
        ckpt_dir / "latest.pt",
    )

install_preemption_handler(checkpoint)
```

A preempted job's terminal state is `preempted` — it is NOT automatically re-queued. To resume from a prior run, the operator resubmits a new job (which gets a fresh `<job_id>` and a fresh empty `JOBD_CHECKPOINT_DIR`) and points it at the old checkpoint via a separate convention env var like `RESUME_FROM=$OLD_CHECKPOINT_DIR/latest.<ext>`. The new run writes its own checkpoints into its own `JOBD_CHECKPOINT_DIR`; the prior run's directory remains on disk until manually cleaned up.
