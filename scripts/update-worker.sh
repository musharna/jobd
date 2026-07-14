#!/usr/bin/env bash
# Pull-based worker CD. The mirror of scripts/deploy-broker.sh, for the half of
# the fleet that had no deployment story at all.
#
# WHY THIS EXISTS
# ---------------
# The broker self-deploys; the workers did not. They were upgraded only when a
# human remembered to restart them, so they drifted — and a fix that never
# reaches a worker is not a fix. Concretely: the `cwd_missing` pre-dispatch check
# shipped in v0.5.10 on 2026-06-30 and did nothing for twelve days, because the
# workers were still on 0.5.3/0.5.7. Nine jobs died of the exact fault it had
# been written to prevent, and CI was green the whole time.
#
# THE IDLE GATE
# -------------
# A worker restart is NOT free. On SIGTERM the worker signals its in-flight job
# through the drain hook, gives it JOBD_WORKER_DRAIN_GRACE_S (60s), then abandons
# it -- termination_reason=worker_shutdown. That is how 13 jobs died on 07-13/14,
# every one of them killed by a human upgrading a worker.
#
# So this NEVER restarts a busy worker. It asks the broker (the only thing that
# knows) and defers to the next tick. Workers are idle most of the time; a
# long-running job simply delays its host's upgrade, which is exactly right.
#
# Idempotent, safe to run on a timer, and DRY_RUN=1 changes nothing.
set -euo pipefail

VENV=${JOBD_WORKER_VENV:-$HOME/jobd-worker/.venv}
UNIT=${JOBD_WORKER_UNIT:-job-worker.service}
DRY=${DRY_RUN:-0}

# NB: no apostrophes inside ${VAR:?word} — bash still does quote processing in
# there, so a lone ' opens a quoted region and the script dies at EOF.
: "${JOBD_URL:?JOBD_URL not set - source the worker environment file}"
: "${JOBD_API_TOKEN:?JOBD_API_TOKEN not set}"
HOST=${JOBD_WORKER_HOST:-$(hostname)}

api() { curl -sf -H "Authorization: Bearer ${JOBD_API_TOKEN}" "$@"; }

# --- target = whatever the broker is running ------------------------------
# Deliberately NOT "latest on PyPI": a worker must never run ahead of its broker.
# The broker is the schema authority; matching it is the only version that is
# guaranteed to interoperate.
target=$(api "${JOBD_URL}/health" | python3 -c 'import sys,json;print(json.load(sys.stdin)["version"])')
installed=$("$VENV/bin/python" -c 'import jobd;print(jobd.__version__)' 2>/dev/null || echo "none")

if [ -z "$target" ]; then
  echo "update-worker: could not read the broker version; doing nothing" >&2
  exit 1
fi
if [ "$target" = "$installed" ]; then
  echo "update-worker: $HOST already on $installed"
  exit 0
fi
echo "update-worker: $HOST $installed -> $target"

# --- the gate: never restart a worker that is running a job ---------------
busy() {
  api "${JOBD_URL}/workers" | python3 -c "
import sys, json
me = ${HOST@Q}
for w in json.load(sys.stdin):
    if w['host'] == me:
        print(w.get('running') or 0)
        break
else:
    print(0)   # broker has never seen us: nothing of ours is in flight
"
}

running=$(busy)
if [ "${running:-1}" != "0" ]; then
  echo "update-worker: $HOST is running $running job(s) — deferring. A restart would"
  echo "               SIGTERM them (60s grace, then worker_shutdown). Next tick."
  exit 0
fi

if [ "$DRY" = "1" ]; then
  echo "update-worker: [dry] would pip install jobd[worker]==$target and restart $UNIT"
  exit 0
fi

# Install BEFORE the second idle check: pip is the slow part (seconds), and doing
# it first keeps the window between "still idle?" and "restart" as short as
# possible. Writing into the venv does not disturb the running process.
"$VENV/bin/pip" install -q --upgrade "jobd[worker]==${target}"

# Re-check. The worker long-polls, so it can claim a job at any moment; this
# narrows the race to the width of a systemctl call rather than a pip install.
# It does not eliminate it — if we lose, the job takes the normal 60s drain and
# the broker resurrects it on the next heartbeat (v0.5.25).
running=$(busy)
if [ "${running:-1}" != "0" ]; then
  echo "update-worker: $HOST claimed a job mid-upgrade — venv is staged, restart deferred"
  exit 0
fi

systemctl --user restart "$UNIT"

# Verify the thing actually took. A restart that silently failed to change the
# version is the failure mode this whole script exists to end.
sleep 5
now=$("$VENV/bin/python" -c 'import jobd;print(jobd.__version__)' 2>/dev/null || echo "none")
if [ "$now" != "$target" ]; then
  echo "update-worker: FAILED — $HOST still reports $now after restart (wanted $target)" >&2
  exit 1
fi
echo "update-worker: $HOST now on $now"
