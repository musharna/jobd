# Contributing to jobd

Thanks for your interest in jobd — a self-hostable, GPU-aware job broker (FastAPI + SQLite) with a first-class MCP server. This guide covers how to get a dev environment going, run the checks CI runs, and exercise the broker/worker/CLI/MCP locally.

> **Threat model.** jobd runs arbitrary commands you submit — it is remote-code-execution **by design**. Keep that in mind when changing the matcher, the worker's execution path, or auth. Security-sensitive changes should reference [`SECURITY.md`](SECURITY.md) and [`docs/security.md`](docs/security.md). Please report vulnerabilities privately per `SECURITY.md`, not in a public issue.

## Dev environment setup

jobd uses [`uv`](https://docs.astral.sh/uv/) for dependency management, and CI installs from the committed lockfile. Mirror it locally:

```bash
# install all extras the test/lint/type checks need
uv sync --extra dev --extra worker --extra mcp
```

This installs the `dev` tools (pytest, ruff, mypy, respx, coverage), the `worker` runtime deps (psutil, nvidia-ml-py, pyyaml), and the `mcp` server deps. Requires **Python ≥ 3.11** (CI tests 3.11, 3.12, and 3.13).

If you change dependencies in `pyproject.toml`, refresh the lock with `uv lock` and commit `uv.lock` — CI runs `uv sync --frozen` and will fail on an out-of-date lockfile.

## Running the checks (what CI runs)

CI (`.github/workflows/ci.yml`) runs three things on the 3.11/3.12/3.13 matrix. Run them locally before opening a PR:

```bash
# Lint (gates the build)
uv run ruff check .

# Tests, excluding tests that hit a real broker
uv run pytest -m "not live" -q

# Type-check (informational — does not gate the build yet)
uv run mypy src/jobd src/job_cli
```

### Tests and the `live` marker

Most of the suite runs against in-process fixtures. Tests marked `live` hit a **real running jobd broker** and are **skipped unless `RUN_LIVE_JOBD=1`** (see `[tool.pytest.ini_options]` in `pyproject.toml`). CI deliberately excludes them with `-m "not live"`. To run them locally, start a broker first (see below) and:

```bash
RUN_LIVE_JOBD=1 uv run pytest -m live -q
```

A deploy lint (`tests/test_deploy_lint.py`) enforces that the Docker broker never binds a non-loopback / non-tailnet interface — keep it green when touching deployment config.

### Linting & formatting

Ruff is configured in `pyproject.toml` (`line-length = 100`, `target-version = py311`). `uv run ruff check .` is the gate; `uv run ruff check --fix .` and `uv run ruff format .` apply autofixes.

### Type-checking

`uv run mypy src/jobd src/job_cli` runs in CI but is **non-blocking** (the codebase is mid-typing-adoption). Don't regress it — prefer adding annotations to new/changed code so we can tighten it to a hard gate later.

## Running jobd locally for manual testing

Single-host loop, all from a checkout:

```bash
# 1. broker — binds 127.0.0.1:8765; no-auth is fine for a loopback-only broker
JOBD_ALLOW_NO_AUTH=1 uv run jobd

# 2. worker — in another shell, pointed at the broker
JOBD_URL=http://127.0.0.1:8765 JOBD_WORKER_HOST=local uv run jobd-worker

# 3. CLI — submit and inspect
uv run job submit --project demo --wait -- echo hello
uv run job list
uv run job logs <id>
uv run job submit --project demo --explain -- echo hi   # dry-run the resolution
```

To exercise the MCP server (`jobd-mcp`) against the same broker:

```bash
JOBD_URL=http://127.0.0.1:8765 JOBD_ALLOW_NO_AUTH=1 uv run jobd-mcp
```

With a token-protected broker, set `JOBD_API_TOKEN` to the same value on the broker, worker, CLI, and MCP host (the broker refuses to start without a token unless `JOBD_ALLOW_NO_AUTH=1`).

## Commits & pull requests

- **CI must pass.** Ruff and the `not live` test suite gate every PR across the 3.11–3.13 matrix; mypy is reported but non-blocking.
- Keep changes focused and add tests for new behavior (use the `live` marker only for tests that genuinely need a real broker).
- Update [`CHANGELOG.md`](CHANGELOG.md) for any user-facing change.
- Update docs (`README.md`, `docs/`) when you change install/usage/config surface.

### Branch / mirror note

This repository has a **public branch that mirrors to GitHub `main`**. The public tree is a curated superset of upstream work; not every internal commit is mirrored, and history on `main` is what the published package builds from. Target `main` for PRs and expect maintainer review before merge.
