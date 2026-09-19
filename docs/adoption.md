# Adopting an already-running process

`job adopt --pid N` makes the broker aware of a process that was started
**outside** jobd (a `nohup python train.py &`, a tmux pane, a notebook kernel).
The process becomes a job: it shows in `job list`, holds a worker slot and its
resources, can be depended on, waited on and cancelled — and jobd stops treating
its GPU memory as anonymous foreign load.

Linux only. It needs `/proc`; anywhere else `job adopt` refuses.

```bash
# on the host where the process lives
job adopt --pid 41233 --project sdxl --gpu          # or --vram-gb 16
job submit --project sdxl --gpu --depends-on <id> --depends-on-any-exit -- python eval.py
```

## How it works

1. **CLI, on the process's host**, reads `/proc/<pid>`: the start time
   (`stat` field 22, clock ticks since boot), `cmdline` and `cwd`. It sends
   them to `POST /adopt` with the worker host name (`--host`, default
   `$JOBD_WORKER_HOST` or the hostname — the same rule the worker uses).
2. **Broker** refuses (409) unless that worker is online and advertises the
   `jobd-adopt` tag — a worker too old to understand adoption must not be
   handed one. Otherwise it inserts a job row **directly in `running`**, with
   `worker = host`, `adopt_pid`, `adopt_start_ticks`, `preemptible = false`,
   no retries, no timeouts, and emits `job_adopted`.
3. **Worker** learns of it from the `/heartbeat` response, which now lists the
   host's running adopted jobs (`adopted: [...]`). It launches nothing. It
   opens a pidfd (`os.pidfd_open`), *then* compares the start time, so the fd
   is known to refer to the process the user named; without pidfd support it
   polls `/proc/<pid>/stat` and compares the start time on every look. A PID
   whose start time differs is a different process — PID reuse — and is refused.
4. The job stays `running` while the process lives and goes terminal when it
   exits.

The adopted job is never `queued` or `assigned`, so none of the requeue paths
(sweeper reclaim, reconcile, admission refusal, retry) can ever see it, and
`/next-job` additionally refuses to claim any row carrying `adopt_pid`. The
invariant: **jobd never executes an adopted job's recorded command** — it is a
label read from `/proc`, not something to run.

Because delivery rides on the heartbeat, a restarted worker re-learns its
adopted jobs within one heartbeat and resumes watching them; the start-time
check makes that safe across reboots. If the worker stays down past the
dead-worker window the job is `orphaned` (`worker_died`) like any other, and is
not picked up again when the worker returns; the process is untouched, so adopt
it again.

## What cannot be known

- **The exit code.** Only a parent can `wait()` a process. jobd did not fork
  it, so how it ended is unobservable. An adopted job whose process exits ends
  **`orphaned`** with `termination_reason = adopted_exit_unobserved` and
  `exit_code = null` — "we lost sight of the outcome", which is the truth. It
  is never reported `completed`. Consequence: default-policy dependents are
  cascade-cancelled; "run this after it ends" is `--depends-on ID --depends-on-any-exit`.
- **Its output.** stdout/stderr belong to whoever started it. `job logs` is
  empty for an adopted job.
- **Its start.** `started_at` is the adoption time, not the process's.
- **Its children's VRAM, fully.** The adopted PID and its live descendants
  count as jobd-owned. A child that daemonised away from the tree still reads
  as unregistered.

## Refusals (all loud, all terminal `failed`)

| `termination_reason` | Meaning |
|---|---|
| `adopt_pid_gone` | The worker found no such PID (exited already, or wrong `--host`). |
| `adopt_pid_mismatch` | The PID exists with a different start time: reused, or another machine's PID. |
| `adopt_uid_mismatch` | The process is not owned by the worker's user. The worker could not signal it, so a `job cancel` would be a lie; it refuses to adopt rather than adopt something it cannot cancel. |

## Cancel

`job cancel ID` sends the process SIGTERM, then SIGKILL after the job's grace
(60 s). Only that PID is signalled — never its process group, which may be the
user's interactive shell. Signals go through the pidfd, so they cannot land on
a recycled PID. The job ends `cancelled`. `job preempt` is refused (adopted
jobs are not preemptible); a worker shutdown/drain leaves the process alone.

## VRAM accounting

Before adoption the process's VRAM is reported as `unregistered_vram_gb`, which
the matcher subtracts on top of NVML's already-reduced free figure. After
adoption the PID (and descendants) join the worker's owned set, exactly as a
launched job's PIDs do, so that memory leaves `unregistered_vram_gb`. The job's
declared `vram_gb` reserves only what the process does not already hold
(`max(0, declared − held)`, the existing in-flight rule), so nothing is counted
twice. The adopted job occupies a worker slot; adopting onto a full worker is
allowed (the process exists regardless) and simply keeps the worker from
claiming more until a slot frees.

## Not in scope

No MCP tool (adoption must run on the process's host; the MCP server may not),
no log capture, no adoption of process groups or containers, no non-Linux.
