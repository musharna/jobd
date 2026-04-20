# Phase 4-lite: PreToolUse Nudge Hook — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a PreToolUse hook that nudges (non-blocking) Claude toward `job submit` when it's about to run recognizably-heavy bash commands.

**Architecture:** A single bash script reads the PreToolUse JSON payload from stdin, matches the command against 7 hardcoded regexes, and on first hit appends a TSV line to `~/.claude/jobd-nudges.log` and writes a stderr nudge. The script always exits 0 — it never blocks Bash. A sibling test runner pipes fixtures through the script and asserts on stderr + log contents.

**Tech Stack:** bash, jq, grep -E. No network, no Python. Runs under Claude Code's PreToolUse hook with matcher `Bash`.

**Spec:** `docs/superpowers/specs/2026-04-20-jobd-phase4-nudge-hook-design.md`

---

## File Structure

Develop and commit in the jobd repo, then deploy to `~/.claude/hooks/` as the final task.

- **Create** `hooks/jobd-nudge.sh` — the hook script (reads stdin, matches rules, logs, emits stderr, exits 0).
- **Create** `hooks/test-jobd-nudge.sh` — test runner with the 7 fixtures from the spec.
- **Deploy (copy) to** `~/.claude/hooks/jobd-nudge.sh` — the live hook Claude Code invokes.
- **Modify** `~/.claude/settings.json` — add one PreToolUse object with `"matcher": "Bash"` after the existing Bash destructive-pattern blocker (currently lines 90-99 of the file).

The live log file `~/.claude/jobd-nudges.log` is created lazily by the hook on first match; no task creates it.

## Testability knob

The hook reads `$JOBD_NUDGE_LOG` for its log path, falling back to `$HOME/.claude/jobd-nudges.log`. This lets `test-jobd-nudge.sh` point each test case at a temp file without touching the real log.

---

## Task 1: Scaffold hook + test runner, handle non-match / malformed input

**Files:**

- Create: `/home/mjarnold/jobd/hooks/jobd-nudge.sh`
- Create: `/home/mjarnold/jobd/hooks/test-jobd-nudge.sh`

This task establishes the skeleton: the hook reads stdin defensively and exits 0, the test runner pipes fixtures in, and three cases prove no-match / malformed-JSON / missing-field all produce empty stderr, empty log, exit 0.

- [ ] **Step 1: Write the test runner with three failing fixtures**

Create `/home/mjarnold/jobd/hooks/test-jobd-nudge.sh`:

```bash
#!/usr/bin/env bash
# Unit tests for jobd-nudge.sh. Pipes JSON payloads in and asserts on
# exit code, stderr, and log contents. Each case uses its own temp log.
set -u

HOOK="$(cd "$(dirname "$0")" && pwd)/jobd-nudge.sh"
PASS=0
FAIL=0

run_case() {
	# run_case NAME PAYLOAD EXPECT_STDERR_SUBSTR EXPECT_LOG_SUBSTR
	local name="$1" payload="$2" expect_stderr="$3" expect_log="$4"
	local tmplog stderr exit_code actual_log
	tmplog=$(mktemp)
	rm -f "$tmplog"
	stderr=$(JOBD_NUDGE_LOG="$tmplog" bash "$HOOK" <<<"$payload" 2>&1 >/dev/null)
	exit_code=$?
	actual_log=""
	[[ -f "$tmplog" ]] && actual_log=$(cat "$tmplog")
	rm -f "$tmplog"

	if [[ $exit_code -ne 0 ]]; then
		echo "FAIL [$name]: exit $exit_code (want 0)"
		FAIL=$((FAIL + 1))
		return
	fi
	if [[ -n "$expect_stderr" ]]; then
		if ! echo "$stderr" | grep -qF "$expect_stderr"; then
			echo "FAIL [$name]: stderr missing '$expect_stderr' — got: $stderr"
			FAIL=$((FAIL + 1))
			return
		fi
	else
		if [[ -n "$stderr" ]]; then
			echo "FAIL [$name]: expected empty stderr, got: $stderr"
			FAIL=$((FAIL + 1))
			return
		fi
	fi
	if [[ -n "$expect_log" ]]; then
		if ! echo "$actual_log" | grep -qF "$expect_log"; then
			echo "FAIL [$name]: log missing '$expect_log' — got: $actual_log"
			FAIL=$((FAIL + 1))
			return
		fi
	else
		if [[ -n "$actual_log" ]]; then
			echo "FAIL [$name]: expected empty log, got: $actual_log"
			FAIL=$((FAIL + 1))
			return
		fi
	fi
	echo "PASS [$name]"
	PASS=$((PASS + 1))
}

# --- fixtures ---

run_case "non-match-ls" \
	'{"tool_input":{"command":"ls -la"},"session_id":"s1"}' \
	"" ""

run_case "malformed-json" \
	'{not valid}' \
	"" ""

run_case "missing-command-field" \
	'{"session_id":"s1"}' \
	"" ""

echo "----"
echo "PASS: $PASS  FAIL: $FAIL"
[[ $FAIL -eq 0 ]]
```

- [ ] **Step 2: Make the test runner executable and run it — expect failure**

```bash
chmod +x /home/mjarnold/jobd/hooks/test-jobd-nudge.sh
/home/mjarnold/jobd/hooks/test-jobd-nudge.sh
```

Expected: FAIL — `jobd-nudge.sh` doesn't exist yet (bash reports "No such file or directory" and non-zero exit).

- [ ] **Step 3: Write the defensive hook skeleton**

Create `/home/mjarnold/jobd/hooks/jobd-nudge.sh`:

```bash
#!/usr/bin/env bash
# PreToolUse Bash nudge hook for jobd (Phase 4-lite).
#
# Reads the PreToolUse JSON payload from stdin, matches the command against
# a hardcoded regex list, and on first hit writes a stderr nudge + appends
# a TSV log line. Always exits 0 — the hook is advisory, never blocking.
#
# Log path override for tests: $JOBD_NUDGE_LOG
#
# Safety: every failure mode falls through to exit 0. A broken hook must not
# convert all Bash calls into failures.

# Deliberately NOT using set -e: we want every internal failure to be
# non-fatal so Bash calls are never blocked. We also trap any uncaught
# error and force exit 0.
trap 'exit 0' ERR

LOG="${JOBD_NUDGE_LOG:-$HOME/.claude/jobd-nudges.log}"

# Read stdin once.
PAYLOAD=$(cat)

# Parse JSON; on any jq failure, both CMD and SESSION_ID come out empty and
# we silently exit 0 below (no rule can match an empty command).
CMD=$(jq -r '.tool_input.command // empty' <<<"$PAYLOAD" 2>/dev/null || echo "")
SESSION_ID=$(jq -r '.session_id // empty' <<<"$PAYLOAD" 2>/dev/null || echo "")

if [[ -z "$CMD" ]]; then
	exit 0
fi

# (Rule matching is added in Task 2.)

exit 0
```

- [ ] **Step 4: Make the hook executable and run the tests — expect pass**

```bash
chmod +x /home/mjarnold/jobd/hooks/jobd-nudge.sh
/home/mjarnold/jobd/hooks/test-jobd-nudge.sh
```

Expected output ends with `PASS: 3  FAIL: 0`.

- [ ] **Step 5: Commit**

```bash
cd /home/mjarnold/jobd
git add hooks/jobd-nudge.sh hooks/test-jobd-nudge.sh
git commit -m "feat(phase4): nudge hook skeleton with non-match tests

Reads PreToolUse JSON from stdin, handles malformed JSON and missing
command field silently. Rule matching comes in the next task."
```

---

## Task 2: Rule matching framework + r-pipeline rule + TSV logging

**Files:**

- Modify: `/home/mjarnold/jobd/hooks/jobd-nudge.sh`
- Modify: `/home/mjarnold/jobd/hooks/test-jobd-nudge.sh`

Adds the first real rule (`r-pipeline`), the match loop, the stderr nudge format, and the TSV log append. Newlines in the logged command are replaced with spaces so each log event is exactly one line.

- [ ] **Step 1: Add the r-pipeline fixture to the test runner**

Append to `/home/mjarnold/jobd/hooks/test-jobd-nudge.sh` just before the `echo "----"` summary block:

```bash
run_case "r-pipeline-match" \
	'{"tool_input":{"command":"Rscript phelipanche/run_pipeline.R --cfg a.yaml"},"session_id":"s2"}' \
	"r-pipeline" \
	"r-pipeline"
```

- [ ] **Step 2: Run tests — expect the new fixture to fail**

```bash
/home/mjarnold/jobd/hooks/test-jobd-nudge.sh
```

Expected: the first three cases pass, `r-pipeline-match` fails (`stderr missing 'r-pipeline'`), exit non-zero.

- [ ] **Step 3: Add rule matching + logging to the hook**

Replace the `# (Rule matching is added in Task 2.)` comment in `/home/mjarnold/jobd/hooks/jobd-nudge.sh` with:

```bash
# Rules are two parallel arrays: RULE_IDS[i] corresponds to RULE_REGEXES[i].
# Evaluated in order; first match wins. Add new rules by appending to both
# arrays. Keep regexes as ERE (grep -E) syntax.
RULE_IDS=(
	"r-pipeline"
)
RULE_REGEXES=(
	'(^|[[:space:]])Rscript[[:space:]]+.*(pipeline|run_[a-zA-Z_]+)\.R\b'
)

MATCHED_ID=""
for i in "${!RULE_IDS[@]}"; do
	# grep exits 2 on a bad regex; || true keeps us going to the next rule.
	if echo "$CMD" | grep -Eq "${RULE_REGEXES[$i]}" 2>/dev/null; then
		MATCHED_ID="${RULE_IDS[$i]}"
		break
	fi
done

if [[ -z "$MATCHED_ID" ]]; then
	exit 0
fi

# Log the event. Replace newlines with spaces so each event is one line.
# Failure to write the log must not prevent the stderr nudge.
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
CMD_ONELINE=$(printf '%s' "$CMD" | tr '\n\r' '  ')
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
printf '%s\t%s\t%s\t%s\n' "$TS" "$SESSION_ID" "$MATCHED_ID" "$CMD_ONELINE" >>"$LOG" 2>/dev/null || true

# Emit the nudge on stderr. Claude reads this in the tool result.
cat >&2 <<EOF
⚠ jobd: this looks like a $MATCHED_ID job. Prefer:
    job submit --project <your-project> --cwd \$(pwd) [--gpu] [--needs R|python3] --wait -- <cmd>
  Broker: http://100.113.204.41:8765 (hardcoded default, no env var needed).
  (Nudge is non-blocking — proceeding.)
EOF

exit 0
```

- [ ] **Step 4: Run tests — expect all four to pass**

```bash
/home/mjarnold/jobd/hooks/test-jobd-nudge.sh
```

Expected: `PASS: 4  FAIL: 0`.

- [ ] **Step 5: Commit**

```bash
cd /home/mjarnold/jobd
git add hooks/jobd-nudge.sh hooks/test-jobd-nudge.sh
git commit -m "feat(phase4): rule-matching framework + r-pipeline + TSV log

One rule implemented end-to-end: first-match loop, stderr nudge,
single-line TSV append to \$JOBD_NUDGE_LOG. Six more rules in next task."
```

---

## Task 3: Add the remaining 6 rules

**Files:**

- Modify: `/home/mjarnold/jobd/hooks/jobd-nudge.sh`
- Modify: `/home/mjarnold/jobd/hooks/test-jobd-nudge.sh`

Adds the other rules from the spec (`heavy-run-wrap`, `python-train`, `accelerate`, `dvc-repro`, `snakemake`, `ssh-desktop`) with one fixture each. The rule list is kept in the order declared in the spec so the "first match wins" semantics are reproducible.

- [ ] **Step 1: Add six fixtures to the test runner**

Append to `/home/mjarnold/jobd/hooks/test-jobd-nudge.sh` just before the `echo "----"` block, after the existing `r-pipeline-match` case:

```bash
run_case "heavy-run-wrap-match" \
	'{"tool_input":{"command":"heavy-run python eval.py"},"session_id":"s3"}' \
	"heavy-run-wrap" \
	"heavy-run-wrap"

run_case "python-train-match" \
	'{"tool_input":{"command":"python train.py --cfg config.yaml"},"session_id":"s4"}' \
	"python-train" \
	"python-train"

run_case "accelerate-match" \
	'{"tool_input":{"command":"accelerate launch --num_processes 1 run.py"},"session_id":"s5"}' \
	"accelerate" \
	"accelerate"

run_case "dvc-repro-match" \
	'{"tool_input":{"command":"dvc repro"},"session_id":"s6"}' \
	"dvc-repro" \
	"dvc-repro"

run_case "snakemake-match" \
	'{"tool_input":{"command":"snakemake --cores 4 all"},"session_id":"s7"}' \
	"snakemake" \
	"snakemake"

run_case "ssh-desktop-match" \
	'{"tool_input":{"command":"ssh desktop-wsl cd ~/orchid && python train.py"},"session_id":"s8"}' \
	"ssh-desktop" \
	"ssh-desktop"
```

- [ ] **Step 2: Run tests — expect the new fixtures to fail**

```bash
/home/mjarnold/jobd/hooks/test-jobd-nudge.sh
```

Expected: the four existing cases pass, six new cases fail (`stderr missing '<rule_id>'`).

- [ ] **Step 3: Extend the rule arrays in the hook**

Replace the existing two arrays in `/home/mjarnold/jobd/hooks/jobd-nudge.sh` with the full spec list (order matters — `heavy-run-wrap` first so wrapped `heavy-run <anything>` wins before narrower rules fire):

```bash
RULE_IDS=(
	"heavy-run-wrap"
	"r-pipeline"
	"python-train"
	"accelerate"
	"dvc-repro"
	"snakemake"
	"ssh-desktop"
)
RULE_REGEXES=(
	'\bheavy-run\b'
	'(^|[[:space:]])Rscript[[:space:]]+.*(pipeline|run_[a-zA-Z_]+)\.R\b'
	'(^|[[:space:]])python[0-9]?[[:space:]]+.*train\.py\b'
	'(^|[[:space:]])accelerate[[:space:]]+launch\b'
	'(^|[[:space:]])dvc[[:space:]]+repro\b'
	'(^|[[:space:]])snakemake\b'
	'(^|[[:space:]])ssh[[:space:]]+desktop(-wsl)?[[:space:]]+.*(train|pipeline|run_)'
)
```

- [ ] **Step 4: Run tests — expect all ten to pass**

```bash
/home/mjarnold/jobd/hooks/test-jobd-nudge.sh
```

Expected: `PASS: 10  FAIL: 0`.

- [ ] **Step 5: Commit**

```bash
cd /home/mjarnold/jobd
git add hooks/jobd-nudge.sh hooks/test-jobd-nudge.sh
git commit -m "feat(phase4): remaining six rules (heavy-run-wrap, python-train, etc.)

All seven rules from the spec now wired. Rule order matches spec so
first-match semantics are reproducible."
```

---

## Task 4: Multi-line command fixture + newline-replacement verification

**Files:**

- Modify: `/home/mjarnold/jobd/hooks/test-jobd-nudge.sh`

The hook already replaces newlines with spaces via `tr '\n\r' '  '`. This task adds the explicit fixture from the spec to guarantee the one-line-per-event invariant doesn't regress.

- [ ] **Step 1: Add the multi-line fixture**

Append to `/home/mjarnold/jobd/hooks/test-jobd-nudge.sh` just before the `echo "----"` block. Note the embedded literal newline inside the command string — we want the hook to flatten it.

```bash
# Multi-line command: the hook must log a single TSV line (no embedded LF).
# Use jq to safely encode the payload; avoids quoting pain with embedded \n.
multiline_payload=$(jq -nc --arg cmd $'heavy-run python a.py\nheavy-run python b.py' \
	'{tool_input:{command:$cmd},session_id:"s9"}')

tmplog=$(mktemp)
rm -f "$tmplog"
JOBD_NUDGE_LOG="$tmplog" bash "$HOOK" <<<"$multiline_payload" >/dev/null 2>&1
log_line_count=$(wc -l <"$tmplog" 2>/dev/null || echo 0)
log_first_line=$(head -n1 "$tmplog" 2>/dev/null || echo "")
rm -f "$tmplog"

if [[ "$log_line_count" -eq 1 ]] && echo "$log_first_line" | grep -qF "heavy-run python a.py heavy-run python b.py"; then
	echo "PASS [multi-line-flattens]"
	PASS=$((PASS + 1))
else
	echo "FAIL [multi-line-flattens]: lines=$log_line_count first='$log_first_line'"
	FAIL=$((FAIL + 1))
fi
```

- [ ] **Step 2: Run tests — expect all eleven to pass**

```bash
/home/mjarnold/jobd/hooks/test-jobd-nudge.sh
```

Expected: `PASS: 11  FAIL: 0`. (If newline replacement were broken, we'd see `lines=2` and a fail line.)

- [ ] **Step 3: Commit**

```bash
cd /home/mjarnold/jobd
git add hooks/test-jobd-nudge.sh
git commit -m "test(phase4): multi-line command flattens to one TSV line"
```

---

## Task 5: Deploy — install hook + wire settings.json + live smoke

**Files:**

- Copy to: `/home/mjarnold/.claude/hooks/jobd-nudge.sh`
- Modify: `/home/mjarnold/.claude/settings.json` (append a new object to the `hooks.PreToolUse` array, after the existing Bash destructive-pattern blocker)

This task makes the hook live. It installs the script into `~/.claude/hooks/`, adds one PreToolUse Bash entry to `settings.json`, validates the JSON, and manually smoke-tests from a fresh Claude session.

- [ ] **Step 1: Copy the hook into place**

```bash
install -m 755 /home/mjarnold/jobd/hooks/jobd-nudge.sh /home/mjarnold/.claude/hooks/jobd-nudge.sh
ls -l /home/mjarnold/.claude/hooks/jobd-nudge.sh
```

Expected: the file exists with mode `-rwxr-xr-x`, size matches the repo copy.

- [ ] **Step 2: Back up settings.json before editing**

```bash
cp /home/mjarnold/.claude/settings.json /home/mjarnold/.claude/settings.json.bak-phase4
```

If the next step corrupts the file somehow, restore with `cp .../settings.json.bak-phase4 .../settings.json`.

- [ ] **Step 3: Add the new PreToolUse Bash entry with jq**

The existing `hooks.PreToolUse` array has two objects (an `Edit|Write` file-protection entry and a `Bash` destructive-pattern entry). Append a third object that invokes our hook. Using `jq` is safer than hand-editing because it preserves structure and validates on write.

```bash
jq '.hooks.PreToolUse += [{
  "matcher": "Bash",
  "hooks": [{
    "type": "command",
    "command": "bash ~/.claude/hooks/jobd-nudge.sh",
    "timeout": 5
  }]
}]' /home/mjarnold/.claude/settings.json > /home/mjarnold/.claude/settings.json.new \
  && mv /home/mjarnold/.claude/settings.json.new /home/mjarnold/.claude/settings.json
```

- [ ] **Step 4: Verify settings.json is still valid JSON and has the new entry**

```bash
jq '.hooks.PreToolUse | length' /home/mjarnold/.claude/settings.json
jq '.hooks.PreToolUse[-1]' /home/mjarnold/.claude/settings.json
```

Expected:

- First command prints `3` (was 2, now 3).
- Second command prints an object containing `"matcher": "Bash"` and a command string ending in `jobd-nudge.sh`.

- [ ] **Step 5: Dry-run the hook end-to-end from the shell**

Simulate what Claude Code will pipe in, and confirm the live hook writes to the real log and emits the nudge on stderr.

```bash
echo '{"tool_input":{"command":"Rscript phelipanche/run_pipeline.R"},"session_id":"smoke-1"}' \
  | bash /home/mjarnold/.claude/hooks/jobd-nudge.sh
echo "---exit $?---"
tail -n1 /home/mjarnold/.claude/jobd-nudges.log
```

Expected:

- The `⚠ jobd: this looks like a r-pipeline job. Prefer: ...` block appears on stderr.
- `---exit 0---`.
- The last log line contains `smoke-1`, `r-pipeline`, and the Rscript command, separated by tabs.

- [ ] **Step 6: Live smoke from a new Claude Code session**

This step cannot be automated — it's a manual confirmation that the hook fires inside the harness.

1. Open a new Claude Code session (restart or `/clear`).
2. Ask Claude: `Please run the bash command: Rscript /tmp/fake_run_pipeline.R`.
3. In the tool result, confirm the stderr nudge is visible (the `⚠ jobd: this looks like a r-pipeline job` block).
4. Confirm Bash still ran — the command itself will fail ("No such file"), but the hook must not have blocked it.
5. Confirm a new line appeared in `~/.claude/jobd-nudges.log` with rule_id `r-pipeline`.

If all four hold, the hook is live and behaving as designed.

- [ ] **Step 7: Commit the deploy artifacts**

`~/.claude/` isn't tracked by any repo, but the jobd repo now has the canonical copy. Record the deploy by capturing a snapshot of the modified settings.json stanza under `docs/`:

```bash
mkdir -p /home/mjarnold/jobd/docs/ops
jq '.hooks.PreToolUse' /home/mjarnold/.claude/settings.json > /home/mjarnold/jobd/docs/ops/claude-settings-pretooluse-snapshot.json
cd /home/mjarnold/jobd
git add docs/ops/claude-settings-pretooluse-snapshot.json
git commit -m "docs(phase4): snapshot live ~/.claude PreToolUse array after nudge deploy"
```

This snapshot is purely for audit / rollback reference — the live file remains the source of truth.

- [ ] **Step 8: Tag the release**

```bash
cd /home/mjarnold/jobd
git tag phase-4-lite
git log --oneline phase-2..phase-4-lite
```

Expected: the output shows the Phase 4-lite commits from this plan (scaffold, rules, multi-line test, deploy snapshot) sitting on top of the `phase-2` tag.

---

## Post-rollout reminder (not a task)

Per the spec's rollout section, after ~1 month of live use:

```bash
awk -F'\t' '{print $3}' ~/.claude/jobd-nudges.log | sort | uniq -c
```

...to get a histogram of rule fires. Then manually sample ~10 matched sessions to eyeball whether Claude followed up with `job submit`. Use the result to decide: promote to blocking, tune the regex list, or leave as-is.
