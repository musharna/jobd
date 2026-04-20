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

command -v jq >/dev/null 2>&1 || exit 0

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
