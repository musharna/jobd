# changelog.d — per-PR changelog fragments

Every PR with a user-facing change adds **its own file** here instead of
editing `CHANGELOG.md` — so parallel PRs never conflict on the same
`[Unreleased]` lines (the v0.5.26 merge train conflicted five PRs in a row).

## Writing a fragment

Create `changelog.d/<slug>.<category>.md`:

- `<slug>` — short kebab-case name for the change (often the branch name)
- `<category>` — one of `fixed`, `security`, `changed`, `added`,
  `deprecated`, `removed`
- content — one or more markdown bullets, same prose style as past
  `CHANGELOG.md` sections. The first line must start with `- `.

Example — `changelog.d/wait-sse-streaming.fixed.md`:

```markdown
- **`/wait` no longer reads the whole log into memory.** ...
```

## At release time

`python3 scripts/roll-changelog.py <version>` assembles all fragments into a
`## [<version>] — <date>` section under `## [Unreleased]` (categories in the
canonical Fixed / Security / Changed / Added / Deprecated / Removed order,
fragments sorted by filename within a category) and deletes the consumed
files. `[Unreleased]` itself stays empty — `tests/test_changelog_fragments.py`
enforces that, along with the naming and bullet contract above.
