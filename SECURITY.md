# Security Policy

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue for a
suspected vulnerability.

Use GitHub's private vulnerability reporting:
[**Report a vulnerability**](https://github.com/musharna/jobd/security/advisories/new)
(Security → Advisories → Report a vulnerability on the repo).

Include as much as you can: affected version/commit, configuration, a
reproduction, and the impact you observed. You'll get an acknowledgement within
a few days; please allow time for a fix before any public disclosure.

## Threat model — read this first

jobd is **remote-code-execution-as-a-service by design**. A client that can
reach the broker's `/submit` endpoint with a valid token can run arbitrary
commands on a worker, as the worker's user. That is the intended function, not
a vulnerability. The security model is two stacked controls — a trusted network
boundary (a Tailscale tailnet by default) and a shared bearer token — described
in full in [docs/security.md](docs/security.md).

**In scope** (please report):

- Auth bypass: reaching any endpoint without a valid token, token leakage in
  logs/output, timing or comparison weaknesses in token checks.
- The tailnet ACL failing open, or being bypassable via spoofed headers.
- SQL injection, path traversal escaping a worker's `mount_roots`, or any way to
  make a worker run something a token-holder could _not_ have run via the
  documented API.
- Privilege escalation beyond the worker user, or escape from the systemd-run
  resource scope.

**Out of scope** (these are documented, accepted properties — see
`docs/security.md` "What this DOESN'T defend against"):

- Arbitrary command/`env`/`cwd` execution by an authenticated token-holder.
- A compromised tailnet node, or an attacker holding both a tailnet position and
  the token.
- Denial of service by a token-holder (unbounded queue/log growth).

## Supported versions

jobd is pre-1.0; security fixes land on the latest release. Pin a version and
watch releases for advisories.
