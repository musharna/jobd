# Event catalog

Every event the broker or a worker records in `events.jsonl`, by name. The
list is pinned to `KNOWN_EVENTS` in `src/jobd/models.py` by
`tests/test_events_catalog_doc.py`; an event listed in one place but not the
other fails CI. Read them with `job events`, `GET /events`, or the MCP
`jobd_events` tool (whose `event` enum is derived from the same constant).

A name that is NOT in this catalog still reaches `events.jsonl` (hooks may
emit their own), but its Prometheus counter collapses into the `other`
bucket, so an alert written against it never fires.

## Job lifecycle (broker)

| Event | Meaning |
|---|---|
| `job_submitted` | A job row was created (one per array member). |
| `job_dispatched` | The dispatcher assigned the job to a worker. |
| `job_started` | The worker reported the workload running. |
| `job_completed` | The workload reached a terminal state the worker reported (completed / failed / exit code carried in the payload). |
| `job_cancelled` | The job was cancelled by request or by a dependency cascade. |
| `job_orphaned` | Its worker died or restarted while it was in flight; the job is parked for possible resurrection. |
| `job_resurrected` | An orphaned job was re-queued after its worker came back. |
| `job_uncancelled` | A dependency-cascade cancel was reversed because the parent came back. |
| `scheduling_timeout` | The job waited longer than its `scheduling_timeout_s` without a matching worker and was failed. |
| `dispatch_skip` | A dispatch pass considered the job and passed it over (payload names the reason, e.g. no worker advertises a required tag). |
| `admission_blocked` | Admission control refused to dispatch (quota, contention, or exclusion). |
| `auto_preempt` | A higher-priority job caused a preemptible one to be preempted. |
| `checkpoint_complete` | The worker confirmed the workload checkpointed inside its grace window. |
| `cwd_refused` | Submit refused the job because its `cwd` cannot be reached from any eligible worker. |

## Submit-time warnings (broker)

`submit_warning` fires once per job whatever warned; the per-cause events
below fire beside it so an alert can name a single cause.

| Event | Meaning |
|---|---|
| `submit_warning` | The submit response carried at least one warning. |
| `unknown_project` | The typed `--project` was not registered and no `roots:` entry identified `cwd`; the job is priced at `_default`. |
| `preflight_warning` | The preflight check on the command or environment reported something non-fatal. |
| `cwd_route_warning` | `cwd` is reachable from some but not all otherwise-eligible workers. |
| `serialization_warning` | A `depends_on` target was already terminal when the job was submitted. |
| `gpu_contention_warning` | The requested GPU is currently held by another process on the pinned host. |
| `sweep_warning` | The post-commit TOCTOU sweep found and cascaded a dependency that failed during submit. |
| `cwd_identity_applied` | The typed name was not registered, and a project's `roots:` supplied the scheduling identity. Not a warning: payload carries `project`, `project_label`, and `matched_root`. |

## Sweeper and retention (broker)

| Event | Meaning |
|---|---|
| `reclaim_suppressed` | The sweep declined its time-based terminal phases because the broker had not been observing the interval (fresh start or a suspend/stall gap). |
| `jobs_pruned` | Terminal job rows older than the retention window were deleted. |
| `logs_pruned` | Job log files older than the retention window were deleted. |
| `env_scrubbed` | Environment blobs on terminal jobs were replaced with `***` after the at-rest window. |

## Worker lifecycle (broker-observed)

| Event | Meaning |
|---|---|
| `worker_registered` | A worker registered or re-registered. |
| `worker_offline` | A worker missed heartbeats past the offline threshold. |
| `worker_stale` | A worker missed heartbeats past the stale threshold (still expected back). |
| `version_drift` | A worker has run a different version than the broker for the full drift window. |

## Worker-posted

| Event | Meaning |
|---|---|
| `worker_shutdown` | The worker is draining and exiting. |
| `watchdog_fired` | The worker's watchdog escalated a workload that ignored SIGTERM to SIGKILL. |
| `stale_scope_sweep` | The worker found and cleaned a leftover systemd scope from a previous run. |
