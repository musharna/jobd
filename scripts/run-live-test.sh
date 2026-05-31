#!/usr/bin/env bash
# Nightly live integration test wrapper — runs tests/mcp/test_live.py against
# the real broker and appends one line per run to a logfile. The schema-drift
# sentinel from the 2026-04-26 incident.
#
# Designed to run under cron on server. Idempotent, non-interactive, never blocks.
# Exits 0 always so cron MAILTO doesn't spam on transient failures (e.g. the
# broker rebooting); inspect the logfile for run history. The test itself
# fails in <10s if the broker is unreachable (it skips with reason).
#
# Install on server:
#   ln -sf ~/jobd/scripts/run-live-test.sh ~/.local/bin/jobd-live-test
#   crontab -e   # see scripts/cron.example

set -u

REPO="${JOBD_REPO:-$HOME/jobd}"
LOG="${JOBD_LIVE_LOG:-$HOME/jobd/logs/live-test.log}"
JOBD_URL="${JOBD_URL:-http://127.0.0.1:8765}"

mkdir -p "$(dirname "$LOG")"

ts() { date -Is; }

cd "$REPO" || {
	echo "$(ts) ERROR: cannot cd $REPO" >>"$LOG"
	exit 0
}

start_ts=$(ts)
output=$(RUN_LIVE_JOBD=1 JOBD_URL="$JOBD_URL" \
	"$REPO/.venv/bin/pytest" -m live tests/mcp/test_live.py --no-header -q 2>&1)
status=$?

if [ $status -eq 0 ]; then
	summary=$(echo "$output" | tail -1)
	echo "$start_ts PASS $summary" >>"$LOG"
else
	echo "$start_ts FAIL exit=$status" >>"$LOG"
	echo "----- output -----" >>"$LOG"
	echo "$output" >>"$LOG"
	echo "----- end output -----" >>"$LOG"
fi

exit 0
