#!/usr/bin/env bash
# Run the README quickstart VERBATIM as a stranger would, in a pristine
# container, against whatever is actually published on PyPI.
#
# Why this exists: on 2026-07-26 this check found that `pip install jobd &&
# jobd` had crashed on boot on EVERY released version since the first commit.
# JOBD_DB_URL defaulted to `sqlite:////app/data/jobd.db` — the container's own
# path — and SQLite will not create missing parent directories. Nothing caught
# it for eight weeks because every other check supplied the value a real user
# does not have: the Dockerfile sets JOBD_DB_URL and mkdirs /app/data, CI sets
# it, and even the test named `test_broker_boots_and_runs_a_job_with_no_config_
# at_all` passed `db_url=` straight to build_app. The previous launch dry-run
# missed it too, because it ran the Docker IMAGE while the README documents pip.
#
# The rule this encodes: a real-execution check must consume the same inputs
# the documented path consumes. Supplying one "to make the test work" silently
# deletes the coverage you thought you bought.
#
# Run it before every release, and after publishing:
#
#     scripts/quickstart-dry-run.sh                 # against published PyPI
#     scripts/quickstart-dry-run.sh dist/jobd-*.whl # against a local build
#
# Requires docker. Exits non-zero on the first failed step, naming it.

set -uo pipefail

WHEEL="${1:-}"
IMAGE="${QUICKSTART_PY_IMAGE:-python:3.11-slim}" # the documented floor
WORKDIR="/root"                                  # a real user's $HOME, never /

command -v docker >/dev/null || {
        echo "quickstart-dry-run: docker is required" >&2
        exit 2
}

mounts=()
install_cmd='pip install --no-cache-dir jobd'
if [ -n "$WHEEL" ]; then
        [ -f "$WHEEL" ] || {
                echo "quickstart-dry-run: no such wheel: $WHEEL" >&2
                exit 2
        }
        # The bind mount MUST preserve the canonical wheel filename — pip rejects a
        # renamed wheel with "not a valid wheel filename", and with --quiet that
        # failure is silent, so PyPI's version gets installed and you test the wrong
        # code. (Cost an hour on 2026-07-26.)
        mounts=(-v "$(cd "$(dirname "$WHEEL")" && pwd):/tmp/dist:ro")
        install_cmd="pip install --no-cache-dir \"/tmp/dist/$(basename "$WHEEL")[worker,mcp]\""
fi

read -r -d '' SCRIPT <<'INNER'
set -uo pipefail
fail() { echo "!!! FAILED at step $1: $2"; exit 1; }
step() { echo; echo "=== STEP $1: $2 ==="; }

step 0 "pristine environment"
python --version
echo "  JOBD_* variables set: $(env | grep -c '^JOBD_' || true)   (must be 0)"
[ "$(env | grep -c '^JOBD_' || true)" -eq 0 ] || fail 0 "environment is not pristine"

step 1 "install"
# Never discard the install output. A swallowed pip error is how the 0.5.34
# investigation lost an hour: a bind mount had renamed the wheel, pip refused
# it with "not a valid wheel filename", --quiet hid that, and PyPI's build got
# installed instead — so the fix appeared not to work when it had never run.
if ! eval "$INSTALL_CMD" >/tmp/install.log 2>&1; then
  echo "--- install output (tail) ---"
  grep -viE 'already satisfied|^ *Downloading|^ *Using cached|^ *━' /tmp/install.log | tail -15
  fail 1 "install failed"
fi
echo "  version: $(python -c 'import jobd; print(jobd.__version__)')"
command -v jobd >/dev/null || fail 1 "'jobd' entrypoint missing"
command -v job >/dev/null || fail 1 "'job' entrypoint missing"

step 2 "start the broker exactly as the README says"
JOBD_ALLOW_NO_AUTH=1 nohup jobd >/tmp/broker.log 2>&1 &
BROKER=$!
for _ in $(seq 1 60); do
  curl -sf http://127.0.0.1:8765/livez >/dev/null 2>&1 && break
  kill -0 "$BROKER" 2>/dev/null || { tail -25 /tmp/broker.log; fail 2 "broker died on boot"; }
  sleep 0.5
done
curl -sf http://127.0.0.1:8765/livez >/dev/null 2>&1 || { tail -25 /tmp/broker.log; fail 2 "broker never became live"; }
echo "  broker live"

step 3 "install + start a worker"
pip install --no-cache-dir "jobd[worker]" >/dev/null 2>&1 || fail 3 "worker extra failed to install"
JOBD_URL=http://127.0.0.1:8765 JOBD_WORKER_HOST=local nohup jobd-worker >/tmp/worker.log 2>&1 &
WORKER=$!
for _ in $(seq 1 60); do
  curl -sf http://127.0.0.1:8765/workers 2>/dev/null | grep -q '"host"' && break
  kill -0 "$WORKER" 2>/dev/null || { tail -25 /tmp/worker.log; fail 3 "worker died on boot"; }
  sleep 0.5
done
curl -sf http://127.0.0.1:8765/workers 2>/dev/null | grep -q '"host"' || { tail -25 /tmp/worker.log; fail 3 "worker never registered"; }
echo "  worker registered"

step 4 "job submit --project demo --wait -- echo hello"
OUT=$(timeout 120 job submit --project demo --wait -- echo hello 2>&1) || { echo "$OUT"; fail 4 "submit failed"; }
echo "$OUT" | grep -q hello || { echo "$OUT"; fail 4 "job output did not contain 'hello'"; }
echo "  job ran and returned its output"

step 5 "job list / job workers"
timeout 60 job list >/dev/null 2>&1 || fail 5 "job list failed"
timeout 60 job workers >/dev/null 2>&1 || fail 5 "job workers failed"
echo "  both answer"

step 6 "the mcp extra the README advertises"
pip install --no-cache-dir "jobd[mcp]" >/dev/null 2>&1 || fail 6 "mcp extra failed to install"
command -v jobd-mcp >/dev/null || fail 6 "'jobd-mcp' entrypoint missing"
echo "  jobd-mcp present"

kill "$BROKER" "$WORKER" 2>/dev/null
echo
echo "ALL QUICKSTART STEPS PASSED"
INNER

docker run --rm "${mounts[@]}" -w "$WORKDIR" \
        -e INSTALL_CMD="$install_cmd" \
        "$IMAGE" \
        bash -c "apt-get update -qq >/dev/null 2>&1; apt-get install -y -qq curl >/dev/null 2>&1; $(printf '%s' "$SCRIPT")"
