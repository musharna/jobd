# Security model

## Threat model

jobd is **remote-code-execution-as-a-service by design**: clients POST `{cmd, cwd, env}` and a worker runs it under your user. The system has two stacked access controls:

1. **Tailnet membership** — the broker runs with `network_mode: host` and uvicorn binds directly to `JOBD_HOST` (a Tailscale CGNAT address) on the host network namespace. Only requests arriving on the tailscale interface reach the broker. Source IPs are preserved, so `TailnetACLMiddleware` (`src/jobd/auth.py`) can verify each request is from `100.64.0.0/10` or loopback. `tests/test_deploy_lint.py` fails CI if either `network_mode: host` or a safe `JOBD_HOST` ever changes.

   **Why host networking, not port-publish:** Docker's default userland-proxy NATs all cross-host inbound traffic to the bridge gateway IP before it reaches the container, which would defeat the source-IP ACL. Host networking sidesteps the proxy entirely.

2. **Shared bearer token** — every request needs `Authorization: Bearer <JOBD_API_TOKEN>`. The broker fails-loud at startup if neither `JOBD_API_TOKEN` nor `JOBD_ALLOW_NO_AUTH=1` is set.

Both layers must be in place. Either alone is insufficient: a `JOBD_HOST=0.0.0.0` or public-interface bind defeats (1); a leaked token over a tailnet you don't fully trust defeats (2).

## Environment variables

| Var                        | Where                    | Required   | Notes                                                                                                                                                                                                                                                                              |
| -------------------------- | ------------------------ | ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `JOBD_API_TOKEN`           | broker, CLI, worker, MCP | yes (prod) | Shared secret. Same value on every host. ≥32 random bytes recommended (`openssl rand -hex 32`).                                                                                                                                                                                    |
| `JOBD_ALLOW_NO_AUTH`       | broker only              | no         | Set `=1` to opt into an unauthenticated broker (tests, local dev). If both this and `JOBD_API_TOKEN` are set, `JOBD_ALLOW_NO_AUTH` wins.                                                                                                                                           |
| `JOBD_DISABLE_TAILNET_ACL` | broker only              | no         | Set `=1` to bypass the source-IP check. Tests use this. The broker **refuses to start** if this is `=1` together with `JOBD_ALLOW_NO_AUTH=1` and a non-loopback `JOBD_HOST` (no auth + no ACL + network bind = unauthenticated RCE). With auth on, `=1` + non-loopback only warns. |
| `JOBD_HOST`                | broker only              | yes (prod) | Interface uvicorn binds to. MUST be loopback (`127.0.0.1`) or a tailscale CGNAT address (`100.x.y.z`) — never `0.0.0.0` or a public IP. Compose defaults to `127.0.0.1`; set `JOBD_HOST` to your host's tailscale IP for cross-host access.                                        |

## Rotation

The full `JOBD_*` configuration catalog (every variable, all components) is in [configuration.md](configuration.md); this page covers only the security-relevant subset in depth.

Token rotation is a coordinated push: update the broker's `.env` first, then push the new token to each worker's systemd unit Environment, then update each Claude CLI / MCP wrapper's env. There is no overlap window — workers will 401 against the new broker until they're updated. Plan rotation for a queue-quiet moment.

**The token is not inherited by workloads.** The worker drops `JOBD_API_TOKEN` from every job's environment before launch — any script, framework, or crash reporter inside a workload can dump its env, and the shared credential must not ride along. The jobd facilities a workload legitimately uses arrive separately (`JOBD_CHECKPOINT_DIR`, `JOBD_CHECKPOINT_GRACE_S`). A job that genuinely needs broker API access opts in explicitly: `env: {"JOBD_API_TOKEN": "..."}` at submit.

## What this DOESN'T defend against

- A compromised tailnet node — full RCE on every worker. (Tier 2: per-worker pre-shared keys.)
- A malicious submitter setting `cwd: "/etc"` or `env: {LD_PRELOAD: "..."}`. Submitted `env` **is** applied to the workload (it layers over the worker's environment; jobd-internal `JOBD_*` vars take precedence) — on a trusted tailnet this is no extra privilege, since a submitter who can reach `/submit` already has RCE. (Tier 2: cwd allowlist + env allowlist.)
- A flood-DoS by a holder of the token. (Tier 3: per-IP rate limit.)
- An attacker who has both a tailnet node AND the token — equivalent to having shell on the worker user.

> **Note on `env` visibility:** env **values** are masked on every job-info read surface — `GET /jobs`, `GET /jobs/{id}`, the submit/cancel/preempt responses, and `jobd_status`/`jobd_job_get` all return `{"KEY": "***"}` (keys stay visible so you can see which vars a job sets, values do not). The real values are delivered only to the claiming worker via `/next-job`, so the job still runs with them. This keeps a submitted token from leaking to other token-holders or into an agent's context via a status read. Two caveats remain: (1) env is stored plaintext at rest in the `jobs.env_json` column only while the job can still be dispatched — once a job is terminal for more than `JOBD_ENV_SCRUB_HOURS` (default 1; negative disables), the sweeper masks the stored values to `{"KEY": "***"}` too, so a secret does not sit in the SQLite file (and every backup of it) forever. Until that scrub lands, anyone with read access to the DB file sees the values; (2) the worker applies it to the workload, so a malicious submitter's `env` is still an RCE vector on a trusted tailnet (above). For long-lived secrets, prefer worker-side env or a secrets file the workload reads itself.
