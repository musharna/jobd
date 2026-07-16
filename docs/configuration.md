# Configuration reference — every `JOBD_*` environment variable

All runtime configuration is environment variables, read at the point of use
(most sweep/retention knobs re-read per pass, so they can be changed without a
restart of anything but the process that owns them). This page is the complete
catalog: `tests/test_config_catalog.py` sweeps the source for `JOBD_*`
literals and fails CI if a variable ships undocumented here — or stays
documented after it stops existing.

Security-sensitive variables (`JOBD_API_TOKEN`, `JOBD_ALLOW_NO_AUTH`,
`JOBD_DISABLE_TAILNET_ACL`, `JOBD_HOST`) are covered in depth in
[security.md](security.md); the rows here are summaries.

## Broker (`jobd`)

| Variable | Default | Purpose |
| --- | --- | --- |
| `JOBD_HOST` | `127.0.0.1` | Interface uvicorn binds to. Set to the tailscale IP in production; the deploy-lint keeps the committed compose value loopback/CGNAT-only. |
| `JOBD_PORT` | `8765` | Port to bind. The CD scripts read it from `.env` so the health gate follows a non-default port. |
| `JOBD_CONFIG_DIR` | `/app/config` | Directory holding `projects.yaml` / `profiles.yaml` / `classifier.yaml` (read-only mount in Docker). |
| `JOBD_DB_URL` | `sqlite:////app/data/jobd.db` | SQLAlchemy URL for the jobd database. |
| `JOBD_DB_POOL_SIZE` | `20` | Engine connection-pool size (file-backed DBs only). |
| `JOBD_DB_MAX_OVERFLOW` | `60` | Engine pool max-overflow (file-backed DBs only). |
| `JOBD_LOGS_DIR` | `./logs` | Per-job stdout/stderr log directory; also holds `events.jsonl`. |
| `JOBD_STATE_DIR` | unset | Mutable broker state (runtime project-priority overlay). Unset → the directory of a file-backed SQLite DB, else the logs dir — never the read-only config dir. |
| `JOBD_API_TOKEN` | — (required in prod) | Shared bearer secret; broker refuses to start without it unless `JOBD_ALLOW_NO_AUTH=1`. See [security.md](security.md). |
| `JOBD_ALLOW_NO_AUTH` | off | `=1` opts into an unauthenticated broker (tests, local dev). |
| `JOBD_DISABLE_TAILNET_ACL` | off | `=1` disables the source-IP ACL middleware (tests, local dev). |
| `JOBD_EVENTS_MAX_BYTES` | 50 MB | `events.jsonl` rotation threshold (~2× retention: current file + one backup). Non-positive/garbage → default, fail-soft. |
| `JOBD_METRICS_CACHE_TTL_S` | `5` | TTL for the `/metrics` GROUP-BY count cache (scrape-amplification guard); `0` disables caching. |
| `JOBD_JOB_RETENTION_DAYS` | `0` (keep forever) | Delete terminal job rows + their logs older than this. Opt-in so history is never silently lost. |
| `JOBD_LOG_RETENTION_DAYS` | `60` | Unlink per-job `.log` files of old terminal jobs, keeping the rows; `0` disables. |
| `JOBD_ENV_SCRUB_HOURS` | `1` | Mask a terminal job's stored `env` values (keys kept) this many hours after it finishes; negative disables. See [security.md](security.md). |
| `JOBD_VERSION_DRIFT_WARN_HOURS` | `24` | Emit one `version_drift` event per episode for an online worker whose version has mismatched the broker's continuously this long; negative disables. |
| `JOBD_STALE_WORKER_THRESHOLD_S` | `60` | Heartbeat age past which an online worker is marked `stale` (fully-dead workers go `offline` on their own clock). |
| `JOBD_CTEST_PARSE` | off | `=1` enables the ctest cost-data ETA predictor for `ctest -R` jobs. |

## Worker (`jobd-worker`)

| Variable | Default | Purpose |
| --- | --- | --- |
| `JOBD_URL` | `http://127.0.0.1:8765` | Broker base URL. |
| `JOBD_API_TOKEN` | — | Bearer token for broker requests. Never inherited by workloads (see [security.md](security.md)). |
| `JOBD_LOG_LEVEL` | `INFO` | Worker log level; unknown names fall back to INFO with a warning instead of crash-looping the service. |
| `JOBD_WORKER_HOST` | machine hostname | Name this worker registers under. |
| `JOBD_WORKER_ALIASES` | none | Comma-separated extra names `host_pin` can match. |
| `JOBD_WORKER_TAGS` | none | Comma-separated capability tags appended to the auto-detected set. |
| `JOBD_WORKER_CONFIG` | `~/.config/jobd/worker.yaml` | Worker config file (tags etc.); wins over auto-detect and `JOBD_WORKER_TAGS`. |
| `JOBD_WORKER_MOUNT_ROOTS` | auto-detected | Comma-separated mount-root prefixes advertised for cwd routing; overrides detection entirely. |
| `JOBD_WORKER_MAX_CONCURRENT_JOBS` | `1` | In-flight job slots. |
| `JOBD_WORKER_IDLE_TIMEOUT_S` | unset (unbounded) | Worker-wide default idle watchdog: kill a job with no new output for this long. Per-job `idle_timeout_s` wins. |
| `JOBD_WORKER_MAX_WALL_S` | unset (unbounded) | Worker-wide default wall-clock cap. Per-job `max_wall_s` wins. |
| `JOBD_WORKER_FIRST_OUTPUT_TIMEOUT_S` | unset (disabled) | Kill a job that never produces a first byte within this window; disarms permanently on first output. |
| `JOBD_WORKER_WATCHDOG_KILL_GRACE_S` | `60` | SIGTERM→SIGKILL grace for watchdog-initiated kills. |
| `JOBD_WORKER_DRAIN_GRACE_S` | `60` | Per-job grace cap during SIGTERM drain, so one slow checkpoint can't pin shutdown past the systemd stop timeout. |
| `JOBD_WORKER_MEM_MAX` | `14G` | `MemoryMax` for the per-job systemd scope. |
| `JOBD_WORKER_SWAP_MAX` | `4G` | `MemorySwapMax` for the per-job systemd scope. |
| `JOBD_WORKER_CHECKPOINT_ROOT` | `~/.local/share/jobd/checkpoints` | Root under which each job gets its durable checkpoint dir. |

## CLI (`job`) and MCP server (`jobd-mcp`)

| Variable | Default | Purpose |
| --- | --- | --- |
| `JOBD_URL` | `http://127.0.0.1:8765` | Broker base URL (shared with the worker). |
| `JOBD_API_TOKEN` | — | Bearer token (shared with the worker). |
| `JOBD_MCP_LOG_DIR` | `~/.claude/state/jobd-mcp` | MCP tool-call log directory; logging failures never fail the tool call. |

## Provided TO workloads (set by the worker, read by jobs)

| Variable | Purpose |
| --- | --- |
| `JOBD_CHECKPOINT_DIR` | This job's durable checkpoint directory (created 0700 before launch). |
| `JOBD_CHECKPOINT_GRACE_S` | The preemption grace window, so `jobd.client.install_preemption_handler` can compute `time_remaining()` inside a checkpoint function. |

Test-suite gates (`JOBD_LIVE`, `JOBD_E2E`, `RUN_LIVE_JOBD`, and the broker/DB
overrides the live CI job exports) are documented in
[CONTRIBUTING.md](../CONTRIBUTING.md) — they configure tests, not the runtime.
