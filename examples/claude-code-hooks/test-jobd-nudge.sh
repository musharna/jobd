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

run_case "r-pipeline-match" \
	'{"tool_input":{"command":"Rscript project-b/run_pipeline.R --cfg a.yaml"},"session_id":"s2"}' \
	"r-pipeline" \
	"r-pipeline"

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
	'{"tool_input":{"command":"ssh desktop-vm cd ~/orchid && python train.py"},"session_id":"s8"}' \
	"ssh-desktop" \
	"ssh-desktop"

# Negative case: path-like substring must NOT match heavy-run-wrap
# (regression guard for the \b false-positive bug).
run_case "heavy-run-path-suffix-no-match" \
	'{"tool_input":{"command":"ls -la /opt/my-heavy-run/bin"},"session_id":"sA"}' \
	"" ""

# Positive case: modern python version must match python-train
# (regression guard for python3.11+ support).
run_case "python311-train-match" \
	'{"tool_input":{"command":"python3.11 train.py --cfg config.yaml"},"session_id":"sB"}' \
	"python-train" \
	"python-train"

# Positive case: anaconda absolute-path python must match python-train.
# Covers real invocations like those emitted by `conda run` or scripts that
# invoke the env's python directly without prior activation.
run_case "anaconda-path-python-train-match" \
	'{"tool_input":{"command":"/opt/conda/envs/ml/bin/python train.py --cfg c.yaml"},"session_id":"sC"}' \
	"python-train" \
	"python-train"

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

echo "----"
echo "PASS: $PASS  FAIL: $FAIL"
[[ $FAIL -eq 0 ]]
