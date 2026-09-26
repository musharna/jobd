#!/usr/bin/env bash
# Unit tests for jobd-nudge.sh. Pipes JSON payloads in and asserts on
# exit code, stderr, and log contents. Each case uses its own temp log.
set -u

HOOK="$(cd "$(dirname "$0")" && pwd)/jobd-nudge.sh"
PASS=0
FAIL=0

run_case() {
	# run_case NAME PAYLOAD EXPECT_STDERR_SUBSTR EXPECT_LOG_SUBSTR [VAR=value ...]
	# Extra VAR=value arguments are set in the hook's environment. The two
	# opt-in rule variables are cleared first so a caller's own settings
	# cannot leak into the cases.
	local name="$1" payload="$2" expect_stderr="$3" expect_log="$4"
	shift 4
	local tmplog stderr exit_code actual_log
	tmplog=$(mktemp)
	rm -f "$tmplog"
	stderr=$(env -u JOBD_NUDGE_WRAPPERS -u JOBD_NUDGE_SSH_HOST_PAT \
		JOBD_NUDGE_LOG="$tmplog" "$@" bash "$HOOK" <<<"$payload" 2>&1 >/dev/null)
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

# Local wrapper rule: opt-in via JOBD_NUDGE_WRAPPERS (space-separated command
# names). Off by default, so the same command must NOT nudge without it.
run_case "wrapper-match-when-configured" \
	'{"tool_input":{"command":"capped-run python eval.py"},"session_id":"s3"}' \
	"local-wrapper" \
	"local-wrapper" \
	JOBD_NUDGE_WRAPPERS="nice-run capped-run"

run_case "wrapper-no-match-by-default" \
	'{"tool_input":{"command":"capped-run python eval.py"},"session_id":"s3b"}' \
	"" ""

# No site-specific wrapper or host is built in: with nothing configured, a
# wrapper-looking command that matches no generic rule does not nudge.
run_case "no-builtin-wrapper-rule" \
	'{"tool_input":{"command":"heavy-run ./eval.sh"},"session_id":"s3c"}' \
	"" ""

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

# Remote GPU host rule: opt-in via JOBD_NUDGE_SSH_HOST_PAT (an ERE matched
# against the ssh host alias). Off by default.
run_case "ssh-gpu-host-match-when-configured" \
	'{"tool_input":{"command":"ssh gpu-box cd ~/proj && ./run_eval.sh"},"session_id":"s8"}' \
	"ssh-gpu-host" \
	"ssh-gpu-host" \
	JOBD_NUDGE_SSH_HOST_PAT="gpu-box"

run_case "ssh-gpu-host-no-match-by-default" \
	'{"tool_input":{"command":"ssh gpu-box cd ~/proj && ./run_eval.sh"},"session_id":"s8b"}' \
	"" ""

run_case "ssh-other-host-no-match" \
	'{"tool_input":{"command":"ssh web-1 cd ~/proj && ./run_eval.sh"},"session_id":"s8c"}' \
	"" "" \
	JOBD_NUDGE_SSH_HOST_PAT="gpu-box"

# Negative case: path-like substring must NOT match a configured wrapper
# (regression guard for the \b false-positive bug).
run_case "wrapper-path-suffix-no-match" \
	'{"tool_input":{"command":"ls -la /opt/my-capped-run/bin"},"session_id":"sA"}' \
	"" "" \
	JOBD_NUDGE_WRAPPERS="capped-run"

# A wrapper name is matched literally: a '.' in it is not a regex wildcard.
run_case "wrapper-dot-is-literal" \
	'{"tool_input":{"command":"axb ./eval.sh"},"session_id":"sD"}' \
	"" "" \
	JOBD_NUDGE_WRAPPERS="a.b"

# An invalid wrapper name is ignored rather than spliced into a regex (spliced,
# "x|ls" would match every `ls`).
run_case "wrapper-invalid-name-ignored" \
	'{"tool_input":{"command":"ls -la"},"session_id":"sE"}' \
	"" "" \
	JOBD_NUDGE_WRAPPERS="x|ls"

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
multiline_payload=$(jq -nc --arg cmd $'python train.py --a\npython train.py --b' \
	'{tool_input:{command:$cmd},session_id:"s9"}')

tmplog=$(mktemp)
rm -f "$tmplog"
env -u JOBD_NUDGE_WRAPPERS -u JOBD_NUDGE_SSH_HOST_PAT \
	JOBD_NUDGE_LOG="$tmplog" bash "$HOOK" <<<"$multiline_payload" >/dev/null 2>&1
log_line_count=$(wc -l <"$tmplog" 2>/dev/null || echo 0)
log_first_line=$(head -n1 "$tmplog" 2>/dev/null || echo "")
rm -f "$tmplog"

if [[ "$log_line_count" -eq 1 ]] && echo "$log_first_line" | grep -qF "python train.py --a python train.py --b"; then
	echo "PASS [multi-line-flattens]"
	PASS=$((PASS + 1))
else
	echo "FAIL [multi-line-flattens]: lines=$log_line_count first='$log_first_line'"
	FAIL=$((FAIL + 1))
fi

echo "----"
echo "PASS: $PASS  FAIL: $FAIL"
[[ $FAIL -eq 0 ]]
