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
