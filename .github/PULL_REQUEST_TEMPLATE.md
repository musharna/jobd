<!-- Thanks for contributing to jobd! -->

## What does this PR do?

<!-- A short description of the change and the motivation. Link any related issue. -->

Fixes #

## Component(s) touched

<!-- Check all that apply -->

- [ ] broker (jobd)
- [ ] worker (jobd-worker)
- [ ] CLI (job)
- [ ] MCP server (jobd-mcp)
- [ ] docs / CI / packaging

## Checklist

- [ ] `uv run ruff check .` passes
- [ ] `uv run pytest -m "not live" -q` passes
- [ ] Added/updated tests for the change (used the `live` marker only where a real broker is genuinely required)
- [ ] Added a `changelog.d/<slug>.<category>.md` fragment for any user-facing change (see `changelog.d/README.md` — do not edit `CHANGELOG.md` directly)
- [ ] Updated docs (`README.md`, `docs/`) if install/usage/config surface changed
