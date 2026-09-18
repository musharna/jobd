- **`job submit --retries N [--retry-delay S]`: opt-in retry for a job that exits
  non-zero.** The default is still 0 — a failed job stays failed, and the broker
  never re-runs a job nobody asked it to. With `--retries`, only a workload that
  exited non-zero *on its own* is put back in the queue; a wall-clock or idle
  timeout, a preempt, a cancel (including one still pending when the failure is
  reported) and launcher/exec faults are never retried. Dependents keep waiting
  while the parent retries and cascade only when an attempt ends it. Each retry
  emits `job_retry_scheduled`; `job status` shows `retries = used/allowed`. Same
  fields on the API and MCP surfaces (`max_retries`, `retry_delay_s`). Adds four
  nullable-safe columns to `jobs`; the additive migration runs at broker start.
