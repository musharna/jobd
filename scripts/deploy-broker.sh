#!/usr/bin/env bash
# Pull-based CD for the jobd broker.
#
# Runs ON the broker host (gt76), on a timer. Resolves the newest published release,
# and if the running broker is behind, pins that version, restarts, and verifies the
# broker actually came back serving the version we asked for — rolling back to the
# previous pin if it did not.
#
# Pull-based on purpose: gt76 has no public ingress, so a push-based CD would mean
# storing a tailnet auth key and an SSH key in GitHub and letting CI reach into the
# homelab. Nothing here needs a secret, and nothing outside the network can trigger it.
#
# Three properties worth stating, because each one is a bug we have actually shipped:
#
#   * It pins an EXACT version, never `latest`. A moving tag cannot be rolled back to,
#     and cannot tell you what is running.
#   * It VERIFIES the deploy by asking the broker its version, not by trusting that
#     `docker compose up` exiting 0 means the new code is serving. (For years a bare
#     `up -d` after a `git pull` silently re-ran the old image.)
#   * It health-gates against the BROKER, not the socket. A TCP connect proves only
#     that something is listening — which is how a healthcheck pointed at the wrong
#     daemon passed for weeks.
#
# Usage:
#   deploy-broker.sh            # deploy the newest release if we are behind
#   deploy-broker.sh 0.5.17     # deploy an exact version (also how you roll back)
#   DRY_RUN=1 deploy-broker.sh  # say what it would do, touch nothing

set -euo pipefail

JOBD_DIR="${JOBD_DIR:-/home/mjarnold/jobd}"
ENV_FILE="$JOBD_DIR/.env"
IMAGE="ghcr.io/musharna/jobd"
METRICS_FILE="${METRICS_FILE:-/var/lib/prometheus/node-exporter/jobd_deploy.prom}"
HEALTH_TIMEOUT_S="${HEALTH_TIMEOUT_S:-90}"
DRY_RUN="${DRY_RUN:-0}"

log() { echo "[$(date -Is)] deploy-broker: $*"; }
die() { log "FATAL: $*"; exit 1; }

[ -f "$ENV_FILE" ] || die "no .env at $ENV_FILE"
# The .env carries JOBD_API_TOKEN; keep it out of everyone else's reach.
# Idempotent, and cheap insurance against a hand-created file (audit 2026-07-15).
chmod 600 "$ENV_FILE" 2>/dev/null || true

# --- Where are we now? Ask the broker, don't infer. -------------------------------
JOBD_HOST=$(grep -E '^JOBD_HOST=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' || true)
JOBD_HOST=${JOBD_HOST:-127.0.0.1}
# The port the broker binds is config too — healthcheck.py reads JOBD_PORT, and a
# health gate probing a hardcoded :8765 on a non-default deployment can never
# pass (audit 2026-07-15).
JOBD_PORT=$(grep -E '^JOBD_PORT=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' || true)
TOKEN=$(grep -E '^JOBD_API_TOKEN=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' || true)
BASE="http://${JOBD_HOST}:${JOBD_PORT:-8765}"

broker_version() {
	# Token via a /dev/fd curl config, not argv (visible in /proc/*/cmdline).
	curl -sS -m 5 -K <(printf 'header = "Authorization: Bearer %s"\n' "$TOKEN") "$BASE/health" 2>/dev/null \
		| python3 -c 'import sys,json; print(json.load(sys.stdin).get("version",""))' 2>/dev/null || true
}

current_pin() { grep -E '^JOBD_TAG=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' || true; }

write_metrics() {
	local ok=$1 version=$2
	[ -d "$(dirname "$METRICS_FILE")" ] || return 0
	local tmp="$METRICS_FILE.tmp"
	{
		echo '# HELP jobd_deploy_last_run_success Whether the last jobd deploy succeeded (1=yes 0=no)'
		echo '# TYPE jobd_deploy_last_run_success gauge'
		echo "jobd_deploy_last_run_success $ok"
		echo '# HELP jobd_deploy_last_success_timestamp Unix time of the last successful jobd deploy'
		echo '# TYPE jobd_deploy_last_success_timestamp gauge'
		if [ "$ok" = 1 ]; then
			echo "jobd_deploy_last_success_timestamp $(date +%s)"
		else
			grep -h 'jobd_deploy_last_success_timestamp ' "$METRICS_FILE" 2>/dev/null | tail -1 \
				|| echo 'jobd_deploy_last_success_timestamp 0'
		fi
		echo '# HELP jobd_deploy_running_version_info Version the broker reports after the last deploy'
		echo '# TYPE jobd_deploy_running_version_info gauge'
		echo "jobd_deploy_running_version_info{version=\"$version\"} 1"
	} > "$tmp"
	mv "$tmp" "$METRICS_FILE"
}

# --- Which version should we be on? ------------------------------------------------
TARGET="${1:-}"
if [ -z "$TARGET" ]; then
	# Newest GitHub release. Unauthenticated: the repo is public, and this keeps the
	# deploy path free of stored credentials.
	TARGET=$(curl -sS -m 15 https://api.github.com/repos/musharna/jobd/releases/latest \
		| python3 -c 'import sys,json; print(json.load(sys.stdin).get("tag_name","").lstrip("v"))' 2>/dev/null || true)
	[ -n "$TARGET" ] || die "could not resolve the latest release from the GitHub API"
fi

# TARGET reaches a sed replacement into the file that holds JOBD_API_TOKEN, and
# it arrives from argv or a remote API. Refuse anything that is not a plain
# version before it touches the .env (audit 2026-07-15): this also keeps
# sed-special characters (| & newline) out of the pin_tag expression.
[[ "$TARGET" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "target '$TARGET' is not a plain X.Y.Z version"

RUNNING=$(broker_version)
PINNED=$(current_pin)
log "running=${RUNNING:-<unreachable>}  pinned=${PINNED:-<unset>}  target=$TARGET"

if [ "$RUNNING" = "$TARGET" ] && [ "$PINNED" = "$TARGET" ]; then
	log "already on $TARGET — nothing to do"
	write_metrics 1 "$RUNNING"
	exit 0
fi

if [ "$DRY_RUN" = "1" ]; then
	log "DRY_RUN: would pin JOBD_TAG=$TARGET, pull $IMAGE:$TARGET, restart, and health-gate"
	exit 0
fi

# --- Pull FIRST. A failed pull must not take the running broker down. ---------------
log "pulling $IMAGE:$TARGET"
docker pull "$IMAGE:$TARGET" >/dev/null || die "pull failed for $IMAGE:$TARGET — broker left untouched"

# --- Pin, restart, verify ----------------------------------------------------------
ROLLBACK_TO="${PINNED:-$RUNNING}"

pin_tag() {
	local tag=$1
	if grep -qE '^JOBD_TAG=' "$ENV_FILE"; then
		sed -i "s|^JOBD_TAG=.*|JOBD_TAG=$tag|" "$ENV_FILE"
	else
		echo "JOBD_TAG=$tag" >> "$ENV_FILE"
	fi
}

pin_tag "$TARGET"
log "restarting broker on $TARGET"
(cd "$JOBD_DIR" && docker compose up -d) || {
	log "compose up failed; rolling back pin to ${ROLLBACK_TO}"
	pin_tag "$ROLLBACK_TO"
	(cd "$JOBD_DIR" && docker compose up -d) || true
	write_metrics 0 "$(broker_version)"
	die "deploy failed at compose up"
}

# The gate: the broker must come back and report the version we asked for. Not "the
# container is up", not "the port accepts" — the running code says so itself.
log "waiting up to ${HEALTH_TIMEOUT_S}s for the broker to report $TARGET"
deadline=$(( $(date +%s) + HEALTH_TIMEOUT_S ))
while [ "$(date +%s)" -lt "$deadline" ]; do
	if [ "$(broker_version)" = "$TARGET" ]; then
		log "OK — broker is serving $TARGET"
		write_metrics 1 "$TARGET"
		exit 0
	fi
	sleep 3
done

# --- Health gate failed: roll back -------------------------------------------------
log "HEALTH GATE FAILED — broker did not report $TARGET within ${HEALTH_TIMEOUT_S}s"
if [ -n "$ROLLBACK_TO" ] && [ "$ROLLBACK_TO" != "$TARGET" ]; then
	log "rolling back to $ROLLBACK_TO"
	pin_tag "$ROLLBACK_TO"
	(cd "$JOBD_DIR" && docker compose up -d) || log "WARNING: rollback compose up failed"
	sleep 5
	log "after rollback, broker reports: $(broker_version)"
else
	log "no previous pin to roll back to — leaving $TARGET in place for inspection"
fi
write_metrics 0 "$(broker_version)"
exit 1
