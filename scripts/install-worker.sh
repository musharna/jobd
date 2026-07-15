#!/usr/bin/env bash
# install-worker.sh — set up a jobd worker on a fresh host.
#
# Usage:
#   JOBD_API_TOKEN=... bash install-worker.sh --broker http://127.0.0.1:8765 --host <name> \
#       [--version X.Y.Z] [--tags tag1,tag2] [--dry-run]
#
# The installed version is PINNED to what the broker is running (asked via
# /health, which needs JOBD_API_TOKEN) or to an explicit --version. There is
# deliberately no unpinned fallback: a worker must never run ahead of its
# broker (the schema authority), and "pip install latest" on a fresh host is
# exactly how one would (audit 2026-07-15). update-worker.sh keeps it in
# lockstep afterwards — install its timer, see the closing instructions.
#
# Assumes: python3.11+, bash. Does NOT require sudo, a clone, or git — the
# worker ships on PyPI as jobd[worker] and runs via the `jobd-worker` command.
# Installs to: $HOME/jobd-worker/ (a venv with jobd[worker] installed)
# Writes config to: $HOME/.config/jobd/worker.yaml

set -euo pipefail

BROKER_URL=""
HOST_NAME=""
EXTRA_TAGS=""
PIN_VERSION=""
DRY_RUN=0

while [[ $# -gt 0 ]]; do
	case "$1" in
	--broker)
		BROKER_URL="$2"
		shift 2
		;;
	--version)
		PIN_VERSION="$2"
		shift 2
		;;
	--host)
		HOST_NAME="$2"
		shift 2
		;;
	--tags)
		EXTRA_TAGS="$2"
		shift 2
		;;
	--dry-run)
		DRY_RUN=1
		shift
		;;
	*)
		echo "unknown arg: $1" >&2
		exit 1
		;;
	esac
done

[[ -z "$BROKER_URL" ]] && {
	echo "--broker required" >&2
	exit 1
}
[[ -z "$HOST_NAME" ]] && HOST_NAME="$(hostname)"

# --- resolve the version to pin (broker's own, or explicit) ----------------
if [[ -z "$PIN_VERSION" && -n "${JOBD_API_TOKEN:-}" ]]; then
	PIN_VERSION=$(curl -sf -K <(printf 'header = "Authorization: Bearer %s"\n' "$JOBD_API_TOKEN") \
		"$BROKER_URL/health" | python3 -c 'import sys,json;print(json.load(sys.stdin)["version"])' || true)
fi
[[ -n "$PIN_VERSION" ]] || {
	echo "cannot determine which jobd version to install: set JOBD_API_TOKEN so this" >&2
	echo "script can ask $BROKER_URL/health, or pass --version X.Y.Z explicitly." >&2
	echo "There is no unpinned fallback — a fresh worker installed from PyPI latest" >&2
	echo "can run AHEAD of its broker, which the fleet invariant forbids." >&2
	exit 1
}
[[ "$PIN_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
	echo "resolved version '$PIN_VERSION' is not a plain X.Y.Z" >&2
	exit 1
}

ARCH="$(uname -m)"
case "$ARCH" in
x86_64 | amd64) ARCH_NORM="x86_64" ;;
aarch64 | arm64) ARCH_NORM="arm64" ;;
armv7l | arm) ARCH_NORM="arm7" ;;
*) ARCH_NORM="$ARCH" ;;
esac

OS_RAW="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "$OS_RAW" in
linux) OS_NORM="linux" ;;
darwin) OS_NORM="darwin" ;;
*) OS_NORM="$OS_RAW" ;;
esac

HAS_NVIDIA="false"
if command -v nvidia-smi >/dev/null 2>&1 || [[ -f /proc/driver/nvidia/version ]]; then
	HAS_NVIDIA="true"
fi

IS_WSL="false"
if [[ -r /proc/version ]] && grep -qi microsoft /proc/version; then
	IS_WSL="true"
fi

DETECTED_TAGS=()
for t in python3 R docker ffmpeg nvidia-smi; do
	if command -v "$t" >/dev/null 2>&1; then DETECTED_TAGS+=("$t"); fi
done
if [[ "$HAS_NVIDIA" == "true" ]]; then DETECTED_TAGS+=("cuda"); fi
if [[ "$IS_WSL" == "true" ]]; then DETECTED_TAGS+=("wsl"); fi

echo "== Detected =="
echo "  host:  $HOST_NAME"
echo "  arch:  $ARCH_NORM"
echo "  os:    $OS_NORM"
echo "  gpu:   $HAS_NVIDIA"
echo "  tags:  ${DETECTED_TAGS[*]}"
if [[ -n "$EXTRA_TAGS" ]]; then echo "  extra: $EXTRA_TAGS"; fi
echo "  broker: $BROKER_URL"
echo "  jobd:   $PIN_VERSION (pinned to the broker)"

if [[ $DRY_RUN == 1 ]]; then
	echo "(dry-run — exiting)"
	exit 0
fi

INSTALL_DIR="$HOME/jobd-worker"
mkdir -p "$INSTALL_DIR" "$HOME/.config/jobd"

# venv
if [[ ! -d "$INSTALL_DIR/.venv" ]]; then
	echo "== Creating venv =="
	python3 -m venv "$INSTALL_DIR/.venv"
fi

"$INSTALL_DIR/.venv/bin/pip" install -U pip >/dev/null

# The worker + capability detection ship inside the jobd package; the [worker]
# extra pulls httpx, psutil, pyyaml, and nvidia-ml-py (pure-Python; harmless on
# non-GPU hosts). Installs the `jobd-worker` console script into the venv.
# Pinned to the broker's version — never PyPI latest (see header).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
"$INSTALL_DIR/.venv/bin/pip" install "jobd[worker]==${PIN_VERSION}"

# Config file
#
# We deliberately DO NOT write a `tags:` block here. jobd/worker/capabilities.py
# REPLACES the auto-detected tag list when `tags:` is set in the yaml
# (`tags_replace if provided` semantics), and the auto-detected list
# includes runtime-computed cuda-Ngb tier tags that an install-time
# snapshot can't see. Writing tags here was the 2026-04-27 #51 bug:
# server's `cuda-8gb` tier vanished on every worker startup.
# Auto-detection re-runs every start; extras go via JOBD_WORKER_TAGS
# (which APPENDS, not REPLACES).
CFG="$HOME/.config/jobd/worker.yaml"
{
	echo "# jobd worker config — auto-detection handles tags on every start."
	echo "# To append extra tags persistently, set JOBD_WORKER_TAGS=foo,bar"
	echo "# in the environment (e.g. systemd unit Environment= line)."
	echo "# To FORCE a tag list (replace auto-detect, suppressing tier tags),"
	echo "# add a 'tags:' block here manually — be aware this disables the"
	echo "# cuda-8gb/12gb/16gb/24gb/32gb tier tags."
	echo "host: $HOST_NAME"
	echo "arch: $ARCH_NORM"
	echo "os: $OS_NORM"
	echo "gpu: $HAS_NVIDIA"
} >"$CFG"

if [[ -n "$EXTRA_TAGS" ]]; then
	echo ""
	echo "== Extra tags requested via --tags =="
	echo "  $EXTRA_TAGS"
	echo "  Add to the worker's environment to apply (the env appends):"
	echo "    export JOBD_WORKER_TAGS='$EXTRA_TAGS'"
	echo "  (or add Environment=JOBD_WORKER_TAGS=$EXTRA_TAGS to the systemd unit)"
fi

echo "== Installed =="
echo "  venv:    $INSTALL_DIR/.venv"
echo "  config:  $CFG"
echo ""
echo "To run manually (test):"
echo "  JOBD_URL=$BROKER_URL JOBD_WORKER_HOST=$HOST_NAME \\"
echo "    $INSTALL_DIR/.venv/bin/jobd-worker"
echo ""
echo "To auto-start (systemd user unit, requires 'sudo loginctl enable-linger \$USER'):"
echo "  cp $SCRIPT_DIR/job-worker.service ~/.config/systemd/user/"
echo "  # then edit ExecStart in the unit to: $INSTALL_DIR/.venv/bin/jobd-worker"
echo "  systemctl --user enable --now job-worker.service"
echo ""
echo "To keep this worker in lockstep with the broker (STRONGLY recommended —"
echo "a worker that never self-updates is how fixes rot undeployed for weeks):"
echo "  cp $SCRIPT_DIR/jobd-worker-update.service $SCRIPT_DIR/jobd-worker-update.timer ~/.config/systemd/user/"
echo "  # edit the unit's Environment= lines for this host, then:"
echo "  systemctl --user enable --now jobd-worker-update.timer"
