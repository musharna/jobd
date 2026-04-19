#!/usr/bin/env bash
# install-worker.sh — set up a jobd worker on a fresh host.
#
# Usage:
#   bash install-worker.sh --broker http://100.113.204.41:8765 --host <name> [--tags tag1,tag2] [--dry-run]
#
# Assumes: python3.10+, bash, curl, git. Does NOT require sudo.
# Installs to: $HOME/jobd-worker/ (venv + job_worker.py + capabilities.py)
# Writes config to: $HOME/.config/jobd/worker.yaml

set -euo pipefail

BROKER_URL=""
HOST_NAME=""
EXTRA_TAGS=""
DRY_RUN=0

while [[ $# -gt 0 ]]; do
	case "$1" in
	--broker)
		BROKER_URL="$2"
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

DEPS="httpx psutil pyyaml"
if [[ "$HAS_NVIDIA" == "true" ]]; then
	DEPS="$DEPS nvidia-ml-py" # note: prefer nvidia-ml-py over pynvml (renamed upstream)
fi
"$INSTALL_DIR/.venv/bin/pip" install $DEPS

# Copy worker + capability modules (expects to run from checkout)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cp "$SCRIPT_DIR/../worker/job_worker.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/../worker/capabilities.py" "$INSTALL_DIR/"

# Config file
CFG="$HOME/.config/jobd/worker.yaml"
{
	echo "# jobd worker config — edit arch/os/tags to override auto-detection."
	echo "host: $HOST_NAME"
	echo "arch: $ARCH_NORM"
	echo "os: $OS_NORM"
	echo "gpu: $HAS_NVIDIA"
	echo "tags:"
	for t in "${DETECTED_TAGS[@]}"; do echo "  - $t"; done
	if [[ -n "$EXTRA_TAGS" ]]; then
		IFS=',' read -ra EXTRA <<<"$EXTRA_TAGS"
		for t in "${EXTRA[@]}"; do echo "  - $t"; done
	fi
} >"$CFG"

echo "== Installed =="
echo "  venv:    $INSTALL_DIR/.venv"
echo "  config:  $CFG"
echo ""
echo "To run manually (test):"
echo "  JOBD_URL=$BROKER_URL JOBD_WORKER_HOST=$HOST_NAME \\"
echo "    $INSTALL_DIR/.venv/bin/python $INSTALL_DIR/job_worker.py"
echo ""
echo "To auto-start (systemd user unit, requires 'sudo loginctl enable-linger \$USER'):"
echo "  cp $SCRIPT_DIR/../worker/job-worker.service ~/.config/systemd/user/"
echo "  systemctl --user enable --now job-worker.service"
