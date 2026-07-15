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

# This fleet has TWO venv shapes, and assuming one broke this script twice:
#   - laptop:            a uv-managed venv with NO pip binary at all.
#   - desktop/gt76/msi:  plain venvs that DO carry their own pip; two of those
#                        hosts have no uv installed anywhere.
# So there is no single install tool that works everywhere. The install step
# below prefers the venv's own pip and falls back to uv — see install_jobd().
# uv is resolved to an ABSOLUTE path (it lives in ~/.local/bin, which is NOT on
# the systemd user-service PATH; a bare `uv` under systemd is a 127). It may be
# absent, and that is fine on a host whose venv has pip — do NOT hard-fail here.
UV=${JOBD_UV:-$(command -v uv || true)}
[ -x "${UV:-}" ] || UV="$HOME/.local/bin/uv"

# NB: no apostrophes inside ${VAR:?word} — bash still does quote processing in
# there, so a lone ' opens a quoted region and the script dies at EOF.
: "${JOBD_URL:?JOBD_URL not set - source the worker environment file}"
: "${JOBD_API_TOKEN:?JOBD_API_TOKEN not set}"
HOST=${JOBD_WORKER_HOST:-$(hostname)}

# The token goes to curl via a config file on a /dev/fd path (process
# substitution), NOT via -H on argv — argv is world-readable in /proc/*/cmdline
# for the lifetime of every timer-driven curl (audit 2026-07-15).
api() {
  curl -sf -K <(printf 'header = "Authorization: Bearer %s"\n' "$JOBD_API_TOKEN") "$@"
}

# --- target = whatever the broker is running ------------------------------
# Deliberately NOT "latest on PyPI": a worker must never run ahead of its broker.
# The broker is the schema authority; matching it is the only version that is
# guaranteed to interoperate.
# `|| true`: under set -e a failed pipeline in this assignment would kill the
# script HERE, making the diagnostic branch below unreachable (audit 2026-07-15).
target=$(api "${JOBD_URL}/health" | python3 -c 'import sys,json;print(json.load(sys.stdin)["version"])' || true)
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
# Counts JOB ROWS (assigned/running, owned by this host), not the worker
# registry's `running` gauge. The gauge is only refreshed by this worker's own
# heartbeat, so a job claimed via long-poll right after a heartbeat is invisible
# to it for up to ~5s — a real gate-miss window. Job rows are written
# synchronously by the /next-job claim itself, so they are authoritative the
# instant a claim exists (audit 2026-07-15 F2).
count_state() {
  api "${JOBD_URL}/jobs?state_filter=$1" | python3 -c "
import sys, json
me = ${HOST@Q}
print(sum(1 for j in json.load(sys.stdin) if j.get('worker') == me))
"
}

busy() {
  local a r
  a=$(count_state assigned) || return 1
  r=$(count_state running) || return 1
  echo $((a + r))
}

# `|| true`: if the broker is unreachable busy() must yield EMPTY (not kill the
# script under set -e), so the ${running:-1} fail-safe below actually engages —
# unknown means busy, never "assume idle and restart".
running=$(busy || true)
if [ "${running:-1}" != "0" ]; then
  echo "update-worker: $HOST is running $running job(s) — deferring. A restart would"
  echo "               SIGTERM them (60s grace, then worker_shutdown). Next tick."
  exit 0
fi

if [ "$DRY" = "1" ]; then
  echo "update-worker: [dry] would install jobd[worker]==$target into $VENV and restart $UNIT"
  exit 0
fi

# Install BEFORE the second idle check: the install is the slow part (seconds),
# and doing it first keeps the window between "still idle?" and "restart" as
# short as possible. Writing into the venv does not disturb the running process.
# Pinned to the broker's version (never PyPI latest).
#
# Prefer the venv's own pip; fall back to uv for a pip-less (uv-managed) venv.
# Requiring uv fleet-wide was the second wrong fix: three hosts have pip and no
# uv, so a uv-only install 127s on exactly the hosts the first version worked on.
install_jobd() {
  if [ -x "$VENV/bin/pip" ]; then
    "$VENV/bin/pip" install -q --upgrade "jobd[worker]==${target}"
  elif [ -x "$UV" ]; then
    "$UV" pip install --python "$VENV/bin/python" -q "jobd[worker]==${target}"
  else
    echo "update-worker: venv has no pip ($VENV/bin/pip) and no uv ($UV) — cannot install" >&2
    return 1
  fi
}
install_jobd

# Re-check. The worker long-polls, so it can claim a job at any moment; this
# narrows the race to the width of a systemctl call (claim rows are visible
# immediately — see the gate above). It does not eliminate it — and losing is
# NOT free: the restart SIGTERMs the job (60s drain), it terminalizes as
# preempted/worker_shutdown — a DELIBERATE terminal the resurrect will not
# undo — and its default-policy dependents cascade-cancel. The previous claim
# here ("the broker resurrects it") was wrong: resurrection only undoes
# worker_died/worker_restarted, never a drain (audit 2026-07-15 F2).
running=$(busy || true)
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
