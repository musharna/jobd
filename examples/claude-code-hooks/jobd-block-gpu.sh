#!/usr/bin/env bash
# EXAMPLE Claude Code PreToolUse(Bash) hook — hard-blocks GPU launches on a
# remote GPU host that bypass jobd, so an agent can't quietly `nohup python`
# straight onto a shared GPU and collide with other work. Companion to the
# advisory jobd-nudge.sh. Exit 2 blocks the Bash call and surfaces the stderr
# message to the model.
#
# This is a template — it is a no-op until you point it at your GPU host:
#
#   export JOBD_GPU_SSH='user@gpu-host'   # ssh target for the live nvidia-smi probe
#   export JOBD_GPU_HOST_PAT='gpu-host|user@gpu-host|100\.64\.0\.5'
#                                         # regex matching that host inside a command
#
# With those unset the hook exits 0 and blocks nothing. Wire it up in
# settings.json under hooks.PreToolUse with matcher "Bash".
#
# Triggers when the command targets the GPU host (JOBD_GPU_HOST_PAT) AND
# launches a GPU-work signature AND is NOT already wrapped in `job submit`.
# Three bypass markers let the agent override deliberately, each audit-logged:
#
#   # NO_GPU         — "doesn't touch CUDA despite matching a pattern"
#                      (e.g. tailing a train script's log). Silent allow.
#   # CONCURRENT_OK  — "yes it's GPU work, but I checked headroom and intend
#                      concurrent execution." Allow + surface a live
#                      `nvidia-smi --query-compute-apps` snapshot.
#   # VRAM=NGB       — "needs N GB free; verify first." Hook probes live
#                      memory.free and allows iff free_gb >= N + 2 (safety
#                      floor). Probe failure fails CLOSED (block).
#
# Audit log (TSV, $JOBD_BLOCK_LOG, default ~/.claude/jobd-blocks.log):
#   <ts>\t<type>\t<matched_pattern>\t<gpu_state_or_dash>\t<cmd_oneline>
#   <type> ∈ {BLOCK, NO_GPU, CONCURRENT_OK, VRAM_OK, VRAM_BLOCK}.
#
# Safety: every parse failure falls through to exit 0. A broken hook must not
# turn every Bash call into a block.

trap 'exit 0' ERR

command -v jq >/dev/null 2>&1 || exit 0

# Not configured → no-op.
[[ -z "${JOBD_GPU_SSH:-}" || -z "${JOBD_GPU_HOST_PAT:-}" ]] && exit 0

# Best-effort POST to broker /events (1s timeout, backgrounded) so a slow or
# unreachable broker never blocks the hook.
_jobd_post_event() {
	# Args: $1 event, $2 outcome, $3 matched_pattern, $4 gpu_state, $5 cmd_oneline
	[[ -z "${JOBD_URL:-}" ]] && return 0
	local cmd_truncated="${5:0:200}"
	local body
	body=$(jq -nc \
		--arg ev "$1" --arg out "$2" --arg mp "$3" --arg gs "$4" \
		--arg cmd "$cmd_truncated" --arg host "$(hostname -s)" \
		'{source:"hook", event:$ev,
          payload:{outcome:$out, matched_pattern:$mp,
                   gpu_state:( if $gs == "" then null else $gs end ),
                   cmd_oneline:$cmd, host:$host}}' 2>/dev/null) || return 0
	curl -s -m 1 -X POST "$JOBD_URL/events" \
		-H 'Content-Type: application/json' --data "$body" </dev/null >/dev/null 2>&1 &
	disown
}

PAYLOAD=$(cat)
TOOL=$(jq -r '.tool_name // empty' <<<"$PAYLOAD" 2>/dev/null || echo "")
CMD=$(jq -r '.tool_input.command // empty' <<<"$PAYLOAD" 2>/dev/null || echo "")

[[ "$TOOL" != "Bash" ]] && exit 0
[[ -z "$CMD" ]] && exit 0

# Already going through jobd — never block, never log.
if echo "$CMD" | grep -qE '(^|[[:space:]]|;|&&|\|\|)job[[:space:]]+submit([[:space:]]|$)'; then
	exit 0
fi

# Targets the configured GPU host?
echo "$CMD" | grep -qE "$JOBD_GPU_HOST_PAT" || exit 0

# GPU-work signatures. Deliberately overlapping so any one triggers the block.
# Add patterns specific to your workloads by appending to this array.
GPU_PATTERNS=(
	'nohup[[:space:]]+[^|]*python'
	'python[0-9.]*[[:space:]]+[^|]*scripts/train_'
	'python[0-9.]*[[:space:]]+[^|]*train_[a-zA-Z0-9_]*\.py'
	'python[0-9.]*[[:space:]]+[^|]*inference_server[a-zA-Z0-9_]*\.py'
	'python[0-9.]*[[:space:]]+[^|]*--gpu([[:space:]]|$)'
	'accelerate[[:space:]]+launch'
	'[Cc][Uu][Dd][Aa]_VISIBLE_DEVICES=[^[:space:]]+[[:space:]]+[^|]*python'
)

MATCHED=""
for pat in "${GPU_PATTERNS[@]}"; do
	if echo "$CMD" | grep -qE "$pat" 2>/dev/null; then
		MATCHED="$pat"
		break
	fi
done

[[ -z "$MATCHED" ]] && exit 0

# Command targets the GPU host, matches a GPU pattern, is NOT wrapped in jobd.
# Decide between BLOCK / NO_GPU / CONCURRENT_OK / VRAM=NGB.

LOG="${JOBD_BLOCK_LOG:-$HOME/.claude/jobd-blocks.log}"
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
CMD_ONELINE=$(printf '%s' "$CMD" | tr '\n\r\t' '   ')
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true

# Bypass: # NO_GPU — attests this isn't CUDA work. Silent allow, logged.
if echo "$CMD" | grep -qE '#[[:space:]]*NO_GPU\b'; then
	printf '%s\tNO_GPU\t%s\t-\t%s\n' "$TS" "$MATCHED" "$CMD_ONELINE" >>"$LOG" 2>/dev/null || true
	_jobd_post_event "hook_bypassed" "no_gpu_explicit" "$MATCHED" "" "$CMD_ONELINE"
	exit 0
fi

# Bypass: # CONCURRENT_OK — attests there's headroom. Probe live compute-apps
# for the audit trail and surface them so the consent is informed.
if echo "$CMD" | grep -qE '#[[:space:]]*CONCURRENT_OK\b'; then
	GPU_STATE=$(timeout 5 ssh -o BatchMode=yes -o ConnectTimeout=3 \
		"$JOBD_GPU_SSH" \
		'nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader' \
		2>/dev/null | tr '\n' ';' | sed 's/;$//')
	[[ -z "$GPU_STATE" ]] && GPU_STATE="(no foreign holders or probe-failed)"
	printf '%s\tCONCURRENT_OK\t%s\t%s\t%s\n' "$TS" "$MATCHED" "$GPU_STATE" "$CMD_ONELINE" >>"$LOG" 2>/dev/null || true
	_jobd_post_event "hook_bypassed" "concurrent_ok" "$MATCHED" "$GPU_STATE" "$CMD_ONELINE"
	cat >&2 <<MSG
[jobd-bypass] # CONCURRENT_OK — proceeding with concurrent GPU work.
Live compute apps on the GPU host: ${GPU_STATE}
(Logged to $LOG)
MSG
	exit 0
fi

# Bypass: # VRAM=NGB — asks the hook to verify headroom. Allow iff
# free_gb >= N + 2 (safety floor). Probe failure fails CLOSED.
if [[ "$CMD" =~ \#[[:space:]]*VRAM=([0-9]+) ]]; then
	REQ_GB="${BASH_REMATCH[1]}"
	GPU_MEM=$(timeout 5 ssh -o BatchMode=yes -o ConnectTimeout=3 \
		"$JOBD_GPU_SSH" \
		'nvidia-smi --query-gpu=memory.free,memory.total --format=csv,noheader,nounits' \
		2>/dev/null | head -n1 | tr -d ' ')
	FREE_MIB="${GPU_MEM%%,*}"
	TOTAL_MIB="${GPU_MEM##*,}"
	if [[ ! "$FREE_MIB" =~ ^[0-9]+$ ]] || [[ ! "$TOTAL_MIB" =~ ^[0-9]+$ ]]; then
		printf '%s\tVRAM_BLOCK\t%s\tprobe-failed\t%s\n' "$TS" "$MATCHED" "$CMD_ONELINE" >>"$LOG" 2>/dev/null || true
		_jobd_post_event "hook_blocked" "vram_probe_failed" "$MATCHED" "probe-failed" "$CMD_ONELINE"
		cat >&2 <<MSG
[jobd-block] # VRAM=${REQ_GB}GB — probe FAILED (could not reach the GPU host's nvidia-smi).
Failing closed: cannot verify headroom, refusing to launch. Retry once the
host is reachable, or use 'job submit --gpu --vram-gb=${REQ_GB}' which has
its own admission gate.
MSG
		exit 2
	fi
	FREE_GB=$((FREE_MIB / 1024))
	TOTAL_GB=$((TOTAL_MIB / 1024))
	NEEDED_GB=$((REQ_GB + 2))
	if ((FREE_GB >= NEEDED_GB)); then
		printf '%s\tVRAM_OK\t%s\tfree=%dGB/total=%dGB/req=%dGB\t%s\n' "$TS" "$MATCHED" "$FREE_GB" "$TOTAL_GB" "$REQ_GB" "$CMD_ONELINE" >>"$LOG" 2>/dev/null || true
		_jobd_post_event "hook_bypassed" "vram_ok" "$MATCHED" "free=${FREE_GB}GB/total=${TOTAL_GB}GB/req=${REQ_GB}GB" "$CMD_ONELINE"
		cat >&2 <<MSG
[jobd-bypass] # VRAM=${REQ_GB}GB — verified headroom, proceeding.
Live: ${FREE_GB} GB free of ${TOTAL_GB} GB total (need ${REQ_GB} + 2 GB floor = ${NEEDED_GB} GB).
(Logged to $LOG)
MSG
		exit 0
	fi
	HOLDERS=$(timeout 5 ssh -o BatchMode=yes -o ConnectTimeout=3 \
		"$JOBD_GPU_SSH" \
		'nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader' \
		2>/dev/null | tr '\n' ';' | sed 's/;$//')
	[[ -z "$HOLDERS" ]] && HOLDERS="(none reported)"
	printf '%s\tVRAM_BLOCK\t%s\tfree=%dGB/total=%dGB/req=%dGB/holders=%s\t%s\n' "$TS" "$MATCHED" "$FREE_GB" "$TOTAL_GB" "$REQ_GB" "$HOLDERS" "$CMD_ONELINE" >>"$LOG" 2>/dev/null || true
	_jobd_post_event "hook_blocked" "vram_saturated" "$MATCHED" "free=${FREE_GB}GB/total=${TOTAL_GB}GB/req=${REQ_GB}GB/holders=${HOLDERS}" "$CMD_ONELINE"
	cat >&2 <<MSG
[jobd-block] # VRAM=${REQ_GB}GB — INSUFFICIENT headroom, refusing.
Live: ${FREE_GB} GB free of ${TOTAL_GB} GB total. Need ${REQ_GB} + 2 GB floor = ${NEEDED_GB} GB.
Current holders: ${HOLDERS}
Either wait for the holder(s) to finish, preempt via 'job preempt <id>', or
rerun once memory frees up. (Logged to $LOG)
MSG
	exit 2
fi

# No bypass marker — block.
printf '%s\tBLOCK\t%s\t-\t%s\n' "$TS" "$MATCHED" "$CMD_ONELINE" >>"$LOG" 2>/dev/null || true
_jobd_post_event "hook_blocked" "no_bypass_marker" "$MATCHED" "" "$CMD_ONELINE"

cat >&2 <<'MSG'
[jobd-block] Blocked: GPU work on the GPU host must go through jobd.

  Use:  job submit --project <name> --cwd $(pwd) --gpu --wait -- <your command>

  Add  --needs python3  or  --needs R  for tool tags as appropriate.

If this is NOT GPU work (false positive), append `# NO_GPU` and retry:
  <your command>  # NO_GPU

If this IS GPU work but you've verified concurrent headroom, append
`# CONCURRENT_OK` — the hook logs a live compute-apps snapshot:
  <your command>  # CONCURRENT_OK

If you know how much VRAM it needs and want the hook to verify room
(free_gb >= N + 2 GB floor), append `# VRAM=NGB`:
  <your command>  # VRAM=12GB
MSG

exit 2
