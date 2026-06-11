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

Token rotation is a coordinated push: update the broker's `.env` first, then push the new token to each worker's systemd unit Environment, then update each Claude CLI / MCP wrapper's env. There is no overlap window — workers will 401 against the new broker until they're updated. Plan rotation for a queue-quiet moment.

## What this DOESN'T defend against

- A compromised tailnet node — full RCE on every worker. (Tier 2: per-worker pre-shared keys.)
- A malicious submitter setting `cwd: "/etc"` or `env: {LD_PRELOAD: "..."}`. Submitted `env` **is** applied to the workload (it layers over the worker's environment; jobd-internal `JOBD_*` vars take precedence) — on a trusted tailnet this is no extra privilege, since a submitter who can reach `/submit` already has RCE. (Tier 2: cwd allowlist + env allowlist.)
- A flood-DoS by a holder of the token. (Tier 3: per-IP rate limit.)
- An attacker who has both a tailnet node AND the token — equivalent to having shell on the worker user.

> **Note on `env` visibility:** submitted env vars are echoed back in job-info reads (`job get`, `jobd_status`/`jobd_job_get`, the API `/jobs/{id}` response). Any token-holder who can read a job can read its env. Don't pass long-lived secrets in `env` if untrusted readers share the token; prefer worker-side env or a secrets file the workload reads itself.
