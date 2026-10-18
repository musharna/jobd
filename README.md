<div align="center">

# jobd

**A self-hostable, GPU-aware job broker for a few machines of your own, with native MCP integration for LLM agents.**<br>
Like [task-spooler](https://manpages.ubuntu.com/manpages/noble/man1/tsp.1.html) or [pueue](https://github.com/Nukesor/pueue), but across all your machines, and VRAM-aware.

[![CI](https://github.com/musharna/jobd/actions/workflows/ci.yml/badge.svg)](https://github.com/musharna/jobd/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/jobd)](https://pypi.org/project/jobd/)
![Python](https://img.shields.io/pypi/pyversions/jobd)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/musharna/jobd/blob/main/LICENSE)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.21636369.svg)](https://doi.org/10.5281/zenodo.21636369)
[![Glama](https://glama.ai/mcp/servers/musharna/jobd/badges/score.svg)](https://glama.ai/mcp/servers/musharna/jobd)

</div>

<!-- mcp-name: io.github.musharna/jobd -->

<p align="center">
  <img src="https://raw.githubusercontent.com/musharna/jobd/main/docs/assets/demo.svg" alt="jobd in action: job fleet status shows four workers and their versions; a GPU job routes to the worker with enough free VRAM and streams back; a stdin batch submits two jobs at once; job logs -f follows one to completion" width="100%">
</p>

<p align="center"><sub>Demo recorded on jobd v0.5.30; output on later versions may differ.</sub></p>

You have a few GPU machines (a workstation, a server, maybe a laptop) on [Tailscale](https://tailscale.com/) or a LAN, and no cloud, Kubernetes or Slurm, nor any wish for one. You want to send training runs and batch jobs from anywhere, have each land on whichever machine has the VRAM free, survive across sessions, and be preempted cleanly when something more important arrives. jobd is a single-process broker that turns those machines into one queue, and an LLM agent can drive it directly.

```bash
# from any machine on your tailnet:
job submit --project myproj --gpu --vram-required 16 --wait -- python train.py
# → routed to whichever worker has ≥16 GB VRAM free, streamed back to your terminal
```

VRAM routing tracks one GPU per host: the worker reports free memory on GPU index 0 only.

## Why it exists

Most schedulers assume a datacenter, and `nohup` or tmux give you no queue at all. jobd fills the gap between "ssh in and run it" and "stand up Slurm":

- **VRAM-fit routing.** Each job goes to a worker whose live free VRAM, RAM, CPUs, capability tags and arch/OS fit it, instead of you guessing which box is free.
- **Preemption with a checkpoint window.** A higher-priority job can preempt a running one: `SIGTERM`, a grace window, then `SIGKILL`. Each job gets a `JOBD_CHECKPOINT_DIR` to save into during that window; saving is the workload's job. A preempted job ends `preempted` and is not re-run; to resume, submit a new job pointed at the checkpoint. See [docs/preemption.md](https://github.com/musharna/jobd/blob/main/docs/preemption.md).
- **Survives sessions.** Jobs live in the broker, not your shell. Submit, close the laptop, check back tomorrow.
- **Agent-native.** An [MCP](https://modelcontextprotocol.io/) server lets an LLM agent (Claude Code, etc.) submit, monitor and babysit jobs as tool calls.
- **Yours.** One broker process on a machine you own. No accounts, no telemetry, no per-GPU-hour billing; broker and workers talk only to each other and to your clients. Only the optional self-update scripts go online: `scripts/update-worker.sh` (installed on a timer by `job fleet add`) installs from PyPI, and `scripts/deploy-broker.sh` queries the GitHub API and pulls the image from GHCR.

## Why not just use…?

| Tool | What it gives you | Why jobd instead |
| --- | --- | --- |
| **`nohup` / `tmux` / ssh** | Runs a command on one box | No queue, VRAM-aware routing, preemption, or record of what ran where |
| **[task-spooler](https://manpages.ubuntu.com/manpages/noble/man1/tsp.1.html)** | A job queue on one machine | jobd queues across all your machines and routes by live VRAM/CPU fit |
| **[Pueue](https://github.com/Nukesor/pueue)** | A mature single-machine queue daemon | Pueue's README puts distributed execution out of scope; jobd is that layer, plus GPU awareness |
| **[HyperQueue](https://github.com/It4innovations/hyperqueue)** | Multi-machine scheduling with HPC roots, single binary | Counts GPUs but doesn't track VRAM; no preemption/checkpoint contract or agent interface |
| **Slurm** | Datacenter-grade scheduling | Heavy to stand up and operate for 2–3 personal boxes |
| **SkyPilot / dstack** | Runs on clouds and your own machines | SkyPilot's "existing machines" mode installs Kubernetes (k3s) on your boxes; dstack wants Docker and passwordless sudo on every host |
| **Modal** | Serverless GPU compute | Runs on Modal's machines, not yours |
| **Ray** | Distributed compute; Ray Jobs runs any shell command on a Ray cluster | You first stand up and run a Ray cluster |

jobd needs one broker process plus a poller per host, with no containers, sudo or Kubernetes. Its niche is the 2–5-GPU homelab: multi-machine **live VRAM-fit** routing, a **preempt/checkpoint window** and a **native MCP interface**, a combination we haven't found in the tools above.

## Install

```bash
pip install jobd               # broker + CLI
pip install "jobd[mcp]"        # adds the MCP server
pip install "jobd[worker]"     # adds the worker daemon (jobd-worker)
```

Requires Python ≥ 3.11. One package ships the broker (`jobd`), the CLI (`job`), the MCP server (`jobd-mcp`) and the worker (`jobd-worker`); the worker's extra dependencies (psutil, nvidia-ml-py) sit behind `[worker]`. `scripts/install-worker.sh` sets up a worker under `~/jobd-worker` with its own venv and a generated config.

## Quickstart (single host)

```bash
# 1. start the broker (binds 127.0.0.1:8765 by default)
JOBD_ALLOW_NO_AUTH=1 jobd          # no-auth is fine for a loopback-only broker

# 2. in another shell, install + start a worker pointed at it
pip install "jobd[worker]"
JOBD_URL=http://127.0.0.1:8765 JOBD_WORKER_HOST=local jobd-worker

# 3. submit a job and wait for it
job submit --project demo --wait -- echo hello
job list
job logs <id>
```

For a multi-host deployment (Docker broker, systemd workers, Tailscale binding, shared auth token), see [docs/security.md](https://github.com/musharna/jobd/blob/main/docs/security.md) and the templates in `docker-compose.yml` and `scripts/`. Adding a worker to a running fleet is one command:

```bash
job fleet add user@newbox      # ssh in, install pinned to the broker's version,
                               # wire systemd units + the self-update timer,
                               # verify it registers. `job fleet status` shows drift.
```

Day-2 operations (health, draining a worker, upgrades, token rotation, backups) are in [docs/runbook.md](https://github.com/musharna/jobd/blob/main/docs/runbook.md).

## How it works

<p align="center">
  <img src="https://raw.githubusercontent.com/musharna/jobd/main/docs/assets/architecture.svg" alt="Architecture: job CLI, jobd-mcp MCP tools, and HTTP/SSE clients talk to the jobd broker (FastAPI — queue, VRAM matcher, priorities, SQLite) over one tailnet; the broker dispatches via long-poll claims and heartbeats to workers A (24 GB GPU), B (8 GB GPU), and C (CPU-only)" width="640">
</p>

Workers **poll** the broker, so nothing connects in to a worker; the broker matches each job against live capacity and hands it back on the poll.

- **Broker:** FastAPI + SQLite, the single source of truth. Holds the queue, runs the matcher, resolves per-project priorities and defaults, and serves a small HTTP API and an SSE stream.
- **Workers:** one per host. Each advertises live capacity by heartbeat, claims jobs it can run, streams logs back and honors preemption. The argv you submit runs as-is (`shell=False`), not through a shell; the exceptions are `job submit --stdin` and the MCP `jobd_submit` tool, which run a command string as `bash -c <string>`.
- **Clients:** the `job` CLI, `jobd-mcp`, or anything that speaks the HTTP API.

Design decisions and the bugs behind them: [docs/ARCHITECTURE.md](https://github.com/musharna/jobd/blob/main/docs/ARCHITECTURE.md).

## CLI

```
job submit -p PROJ [--gpu] [--vram-required N] [--needs TAG]... [--count N | --sweep K=v1,v2]... [--wait] -- CMD...
job list [--state STATE] [--project P] [--array A<id>]   # queue + recent jobs
job status ID | A<id> [--watch]             # one job, or an array's aggregate
job logs ID [-n BYTES]                      # tail captured output
job wait ID                                 # block until terminal
job cancel ID  /  job preempt ID            # stop a job
job adopt --pid N -p PROJ [--gpu]           # register a process you already started (Linux)
job workers                                 # fleet snapshot + health
job projects list | set NAME PRI | nudge NAME DELTA
job audit [--project P] [--since 24h]       # event history
```

`--count N` and `--sweep KEY=v1,v2` submit a job array, `job submit --explain` prints the effective config without enqueuing anything, and `job adopt` makes a process started outside jobd hold its slot and VRAM until it exits. Examples and details: [docs/cli.md](https://github.com/musharna/jobd/blob/main/docs/cli.md).

## Coming from pueue or task-spooler?

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

## MCP / agent integration

`jobd-mcp` exposes the queue as nine tools: `jobd_submit`, `jobd_status`, `jobd_logs`, `jobd_list`, `jobd_cancel`, `jobd_preempt`, `jobd_events`, `jobd_workers`, `jobd_worker_delete`. [docs/agent-cookbook.md](https://github.com/musharna/jobd/blob/main/docs/agent-cookbook.md) walks through fire-and-babysit polling, surviving preemption with checkpoints, sweeps, and asking the broker why a job won't schedule.

For [Claude Code](https://docs.claude.com/en/docs/claude-code):

```bash
claude mcp add jobd --env JOBD_URL=http://127.0.0.1:8765 --env JOBD_API_TOKEN=<your-token> -- jobd-mcp
```

Any other MCP client:

```json
{
  "mcpServers": {
    "jobd": {
      "command": "jobd-mcp",
      "env": {
        "JOBD_URL": "http://127.0.0.1:8765",
        "JOBD_API_TOKEN": "<your-token>"
      }
    }
  }
}
```

`JOBD_API_TOKEN` must match the broker's token, or every call returns 401; omit it only when the broker runs with `JOBD_ALLOW_NO_AUTH=1`.

`examples/claude-code-hooks/` has optional Claude Code hooks that nudge (or hard-block) an agent toward submitting heavy commands through jobd, including a VRAM-aware GPU guard with `# NO_GPU` / `# CONCURRENT_OK` / `# VRAM=NGB` override markers.

## Does it help?

Yes for speed, not for accuracy: with jobd-mcp, Claude answered questions about a live broker about twice as fast as with the `job` CLI, and both got every answer right.

<p align="center">
  <img src="https://raw.githubusercontent.com/musharna/jobd/main/benchmarks/agent_qa/agent_qa.png" alt="Bar chart of right, wrong and declined answers to 41 questions about a live jobd broker, 3 runs per question for each Claude setup. Constant guess with no model: 4 right, 37 wrong. Best constant fit to the answer key: 10 right, 31 wrong. Claude alone: all 123 declined. Claude with the job CLI: 123 of 123 right. Claude with jobd-mcp: 123 of 123 right." width="720">
</p>

We asked Claude Opus 5.5 41 read-only questions about a live broker, 3 times each. With the server the median answer took 6.7 s against 13.6 s (ratio 0.49, 95% CI 0.44 to 0.59) and used 1.1 fewer tool calls on average. Median cost was 24% lower but mean cost was the same, and the questions are too easy to rank the two setups on accuracy (McNemar p = 1).

Method, controls and limitations: [benchmarks/agent_qa](https://github.com/musharna/jobd/blob/main/benchmarks/agent_qa/README.md).

## Supported platforms

| Component                              | Linux   | macOS       | Windows              |
| -------------------------------------- | ------- | ----------- | -------------------- |
| **Broker** (`jobd`)                    | ✅      | ☑️          | ☑️ (WSL recommended) |
| **CLI** (`job`) / **MCP** (`jobd-mcp`) | ✅      | ☑️          | ☑️                   |
| **Worker** (`jobd-worker`)             | ✅ full | ⚠️ degraded | untested             |

✅ = CI-tested on Ubuntu, but CI has no GPU runner (NVIDIA/VRAM paths are tested against mocks) and tests that need a systemd `--user` scope skip there. ☑️ = pure Python and expected to work, but not exercised by CI; please file an issue if it breaks. The worker's memory caps, process reaping and preemption use `systemd-run --user` scopes and cgroups; on a host without a systemd user instance it still runs jobs but silently drops those guarantees, which is fine for a single trusted box but not for hard resource isolation. GPU features need NVIDIA + `nvidia-ml-py`.

## Configuration

- **Config files**, all optional (with none, every job runs at the global default priority): `projects.yaml` sets per-project priority and submit defaults ([docs/projects-yaml.md](https://github.com/musharna/jobd/blob/main/docs/projects-yaml.md)), `profiles.yaml` defines named resource bundles (`--profile gpu-train-large`), and `classifier.yaml` suggests a profile from the command string. They are read from `JOBD_CONFIG_DIR` (default `/app/config`, the Docker image's path; set it when you run from pip). Examples are in this repo's `config/`, not in the pip package.
- **Environment variables:** every `JOBD_*` variable is in [docs/configuration.md](https://github.com/musharna/jobd/blob/main/docs/configuration.md), which a CI test keeps in lockstep with the source.
- **Concurrency:** a worker runs one job at a time unless you raise `JOBD_WORKER_MAX_CONCURRENT_JOBS`; jobs then bin-pack by their VRAM, RAM and CPU reservations, so a CPU-only job and a GPU job can run side by side ([docs/concurrency.md](https://github.com/musharna/jobd/blob/main/docs/concurrency.md)).
- **Retention:** job logs and job records prune on separate clocks (`JOBD_LOG_RETENTION_DAYS`, `JOBD_JOB_RETENTION_DAYS`); see [docs/runbook.md](https://github.com/musharna/jobd/blob/main/docs/runbook.md#joblog-retention). Events: [docs/events.md](https://github.com/musharna/jobd/blob/main/docs/events.md).

## Security

The broker has no auth beyond a shared bearer token, so it is meant to run on a trusted network (loopback or a Tailscale tailnet), never on a public interface. Three stacked controls:

1. **Interface binding.** Set `JOBD_HOST` to `127.0.0.1` (the default) or a Tailscale CGNAT address (`100.64.0.0/10`), not `0.0.0.0`. The broker does not enforce this: it starts on any address and only warns (or refuses) when a non-loopback bind is combined with no-auth. The only check on the bind is a CI lint (`tests/test_deploy_lint.py`) on the shipped Docker deployment; at runtime, control 2 is what holds.
2. **Source-IP check.** Whatever the bind, the broker answers `403` to any request whose source is neither loopback nor in `100.64.0.0/10` (`src/jobd/auth.py`). `JOBD_DISABLE_TAILNET_ACL=1` turns this off.
3. **Bearer token.** Set `JOBD_API_TOKEN` (≥32 random bytes) on every broker, worker, CLI and MCP host. The broker refuses to start without it unless you set `JOBD_ALLOW_NO_AUTH=1`, which is for a loopback-only broker in local dev and tests. With a non-loopback `JOBD_HOST` it exposes an unauthenticated RCE endpoint to your whole tailnet (the broker logs a startup warning). Don't.

**`/livez`, `/readyz` and `/metrics` are exempt from both the source-IP check and the token**, because a generic HTTP monitor cannot send a token. `/metrics` publishes the broker version, job counts by state, and every worker's hostname and version: no commands, cwd, env or project names, but enough to fingerprint the fleet. For these three the `JOBD_HOST` bind is load-bearing, not defense in depth: port-forward the broker and you publish that inventory. Full table: [Unauthenticated surface](https://github.com/musharna/jobd/blob/main/docs/security.md#unauthenticated-surface). Threat model and token rotation: [docs/security.md](https://github.com/musharna/jobd/blob/main/docs/security.md).

## License

MIT; see [LICENSE](https://github.com/musharna/jobd/blob/main/LICENSE).
