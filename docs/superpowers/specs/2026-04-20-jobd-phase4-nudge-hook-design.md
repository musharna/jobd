# Phase 4-lite: PreToolUse Nudge Hook — Design Spec

> Date: 2026-04-20
> Status: approved for implementation
> Predecessors: `2026-04-18-jobd-design.md` (full phase plan), `2026-04-19-jobd-phase2-capabilities.md` (Phase 2)

## Context

jobd Phase 2 shipped capabilities-aware routing (`--gpu`, `--needs`, `--arch`, unmatcheable warnings). Phase 2 verified end-to-end on 2026-04-20: backward compat, warnings, GPU routing all green. The global `~/.claude/CLAUDE.md` now carries a rule pointing Claude toward `job submit` for heavy work.

The **original Phase 3 (`heavy-run` auto-wraps into jobd) is being skipped.** Rationale: `heavy-run` and `jobd` solve different problems (local cgroup memory cap vs. cross-host coordination), they already compose cleanly (jobd workers wrap every command in `heavy-run`), and a classifier inside `heavy-run` would add tuning complexity with thin benefit. The `heavy-run` wrapper stays as-is.

Instead we go directly to **Phase 4-lite**: a PreToolUse hook that _nudges_ Claude toward `job submit` when it's about to run a recognizably-heavy bash command. Non-blocking on day one. After ~1 month of observation, we decide whether to promote the hook to a hard block.

## Goals

1. Claude sees a clear, specific message ("this looks like a heavy job — prefer `job submit ...`") before running a heavy command, in the same turn, so it can self-correct.
2. Zero risk to non-heavy Bash calls. Hook is belt-and-suspenders: any failure mode is silent exit 0.
3. Enough log signal to answer two questions after the trial: "how often did the hook fire?" and "did Claude comply?"
4. Hook behavior is auditable in <30 seconds by opening the script and reading the regex list.

## Non-goals (for this phase)

- Hard blocking. Deferred to post-trial (Phase 4-full).
- Broker-side classifier consultation. The broker has `/classify`, but we deliberately don't call it from the hook — keeps the hook reliable when broker is down, avoids per-call latency on every Bash invocation.
- Dynamic/learned rules. The regex list is hardcoded and edited manually.
- Auto-submit or auto-rewrite. Claude decides whether to re-invoke via `job submit`; the hook only nudges.
- Hooking non-Bash tools (Edit, Write, etc.).

## Architecture

```
Claude Bash call
    │
    │ {tool_input.command, session_id} via stdin (JSON)
    ▼
~/.claude/hooks/jobd-nudge.sh     [PreToolUse, matcher: Bash]
    │
    ├── jq parse → CMD, SESSION_ID
    ├── for each REGEX in RULES:
    │     match CMD → rule_id, append log line, emit stderr nudge, break
    ├── exit 0 (always — match or not)
    ▼
Bash runs. Claude sees stderr nudge in the same turn.
```

Single script. No network. No state beyond an append-only log.

## Components

### 1. Hook script

**Path:** `~/.claude/hooks/jobd-nudge.sh`
**Permissions:** `chmod +x`
**Language:** bash (matches `wiki_session_nudge.sh` sibling)
**Dependencies:** `jq` (already used by other hooks in `settings.json`)

Responsibilities:

- Read the PreToolUse JSON payload from stdin.
- Extract `.tool_input.command` and `.session_id`.
- For each rule (see "Pattern list" below), test the command with `grep -Eq`. On first hit: break.
- On hit: append one TSV line to the log file, write the nudge text to stderr, exit 0.
- On any internal failure (jq error, log write failure, regex error): exit 0 silently. Never block Bash.

### 2. Log file

**Path:** `~/.claude/jobd-nudges.log`
**Format:** TSV, append-only, no header.

```
<ISO8601 timestamp>\t<session_id>\t<rule_id>\t<command>
```

- Timestamp: `date -u +%Y-%m-%dT%H:%M:%SZ`
- `session_id`: from the PreToolUse payload; empty string if missing
- `rule_id`: short slug (e.g. `r-pipeline`, `python-train`)
- `command`: full command as sent to Bash, newlines replaced with spaces to keep one-line-per-event

No rotation at this phase — expected volume is <50 lines/day. If the log exceeds 10k lines during the trial, we'll add `logrotate`.

### 3. settings.json entry

Add **one** object to the existing `hooks.PreToolUse` array (after the Bash destructive-pattern blocker):

```json
{
  "matcher": "Bash",
  "hooks": [
    {
      "type": "command",
      "command": "bash ~/.claude/hooks/jobd-nudge.sh",
      "timeout": 5
    }
  ]
}
```

Runs _after_ the destructive blocker (so destructive commands are already rejected before we reach the nudge). Hooks at the same level all run; the nudge never blocks, so ordering doesn't materially matter — but placing it after keeps the mental model "safety first, nudges second."

## Pattern list (rules)

Hardcoded in the script. Each rule has a stable `rule_id` for log analysis.

| rule_id          | regex (ERE)                                                                          | covers                               |
| ---------------- | ------------------------------------------------------------------------------------ | ------------------------------------ |
| `heavy-run-wrap` | `\bheavy-run\b`                                                                      | legacy `heavy-run <cmd>` invocations |
| `r-pipeline`     | `(^\|[[:space:]])Rscript[[:space:]]+.*(pipeline\|run_[a-zA-Z_]+)\.R\b`               | R pipelines                          |
| `python-train`   | `(^\|[[:space:]])python[0-9]?[[:space:]]+.*train\.py\b`                              | python training scripts              |
| `accelerate`     | `(^\|[[:space:]])accelerate[[:space:]]+launch\b`                                     | HF Accelerate training               |
| `dvc-repro`      | `(^\|[[:space:]])dvc[[:space:]]+repro\b`                                             | DVC pipeline repro                   |
| `snakemake`      | `(^\|[[:space:]])snakemake\b`                                                        | Snakemake runs                       |
| `ssh-desktop`    | `(^\|[[:space:]])ssh[[:space:]]+desktop(-wsl)?[[:space:]]+.*(train\|pipeline\|run_)` | ssh-wrapped heavy                    |

**Deliberately omitted** (to avoid noise in the trial):

- Bare `pytest` — mostly fast, rare that `-m gpu` needs routing
- `nvidia-smi`, `watch`, `htop` — diagnostic, not workload
- Bare `python <script>.py` — too broad, false-positives
- Model-name substrings (sdxl, stylegan) — already covered by training-script rules

Rules are evaluated in the order listed; first match wins.

## Nudge message format

Emitted to stderr on match:

```
⚠ jobd: this looks like a <rule_id> job. Prefer:
    job submit --project <your-project> --cwd $(pwd) [--gpu] [--needs R|python3] --wait -- <cmd>
  Broker: http://100.113.204.41:8765 (hardcoded default, no env var needed).
  (Nudge is non-blocking — proceeding.)
```

Claude reads stderr in the same tool result. The message is specific about _why_ (the rule_id), _what to do_ (the `job submit` template), and _that it wasn't blocked_ (so Claude doesn't think it has to re-invoke).

## Data flow

1. Claude emits `Bash` tool call.
2. Harness runs each PreToolUse matcher. The destructive-pattern blocker runs (exits 0 for non-destructive commands). Then our hook runs.
3. Our hook reads `{"tool_input":{"command":"<cmd>"},"session_id":"<id>"}` from stdin.
4. Script iterates the rule list. On match, writes log line + stderr nudge, exits 0.
5. Claude proceeds with the Bash call. In the tool result, Claude sees any stderr the hook wrote. Claude either (a) lets the command run and notes the suggestion, or (b) cancels/re-invokes via `job submit` — its choice.
6. Log line is persisted for later analysis.

## Error handling

The hook is defensively written — any internal failure must result in exit 0.

- `jq` parse error on malformed stdin → exit 0 (command passes unchanged, no nudge).
- `.tool_input.command` missing/null → exit 0.
- Log file write fails (disk full, permission) → continue to stderr nudge (best-effort), still exit 0.
- Unknown pattern (regex compile error) → `grep` returns non-zero/2, the rule is skipped, next rule tried.
- Any uncaught error → trap forces exit 0.

This matters because the hook runs on _every_ Bash call Claude makes. A broken hook must not convert all bash runs into failures.

## Testing strategy

Unit tests live alongside the script (`~/.claude/hooks/test-jobd-nudge.sh`). Each test pipes a JSON payload into the hook and asserts on stderr + log content.

**Fixtures:**

1. `Rscript phelipanche/run_pipeline.R` → matches `r-pipeline`, stderr contains "r-pipeline", log file gains one line.
2. `python train.py --cfg config.yaml` → matches `python-train`.
3. `dvc repro` → matches `dvc-repro`.
4. `ls -la` → no match, stderr empty, log unchanged.
5. Malformed JSON (`{not valid}`) → exit 0, stderr empty, log unchanged.
6. Missing `command` field (`{}`) → exit 0, stderr empty, log unchanged.
7. Multi-line command (`bash -c "a\nb"`) → log line is single-line (newlines replaced).

Test script exits 0 iff all fixtures pass; expected to run manually during development + once after any regex edit. No CI wiring — this is a personal-config hook.

## Rollout

1. Write and test the script in isolation.
2. Add the settings.json entry.
3. Restart Claude Code (or start a new session).
4. Run one deliberately-matching command (`Rscript some_pipeline.R` in a throwaway session) to confirm the nudge appears in the tool result.
5. Live for 1 month.
6. After 1 month: `awk -F'\t' '{print $3}' ~/.claude/jobd-nudges.log | sort | uniq -c` → histogram of rule fires. Manually sample 10 fires and check whether the next Claude action was a `job submit` (compliance rate, eyeballed).
7. Decide: promote to blocking (exit 2 + bypass via `HEAVY_OK=1`), tune the pattern list, or leave as-is.

## Open questions (none blocking)

- Should the nudge include the _suggested project_ inferred from `$PWD`? Nice-to-have; skipped for v1 to keep the script trivial.
- Should we also hook `SessionStart` to remind Claude of the jobd rule? Probably not — the global `~/.claude/CLAUDE.md` already does this and the rule is short.

## Success criteria

- Hook fires on ≥4 of the 7 rule categories at least once during the trial (indicates coverage is realistic).
- Eyeballed compliance rate ≥50% within the first two weeks (indicates the nudge works without blocking).
- Zero reports of the hook breaking a non-heavy Bash call (indicates defensive error handling works).

If all three hold at the 1-month mark, promote to blocking. If any fail, tune before promoting.
