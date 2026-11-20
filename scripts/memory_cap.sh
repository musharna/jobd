#!/usr/bin/env bash
# Run a command in a memory-capped systemd scope in which the kernel can kill
# one process for memory and the rest carry on.
#
#   bash scripts/memory_cap.sh REPORT -- COMMAND [ARGS...]
#
# Why: a mutant that never stops allocating fills the runner's memory and the
# runner shuts down mid-run ("The runner has received a shutdown signal"), so no
# later step runs, nothing is saved, and the next night meets the same mutant.
# jobd: stream_output's `if not chunk: break` mutated to `if chunk: break` reads
# b"" forever and the test's fake client keeps every post, past 14 GB within a
# minute. In the scope, the kernel kills the biggest process when the scope
# reaches its limit, which is the runaway worker; mutmut records that mutant
# (exit -9, "segfault") and goes on to the next.
#   OOMPolicy=continue  systemd's default for a unit the kernel has OOM-killed a
#                       process of is to stop the whole unit: mutmut with it.
#   MemorySwapMax=0     with swap, the scope pages out instead of reaching its
#                       limit, and the runner thrashes before the kernel acts.
# The limit is MUTMUT_MEMORY_MAX (bytes, or with a K, M or G suffix) when it is
# set, else the host's MemTotal less 4 GiB. A process that needs more than that
# would have run the runner itself out of memory without the cap; the 4 GiB is
# for the runner's own processes and the page cache outside the scope.
#
# The scope comes from the systemd user manager when there is one (a desktop
# session), else from the system manager through passwordless sudo (a
# GitHub-hosted runner, which has no user session). Under sudo the command is
# started as this user again, with this exact environment and groups.
# If neither can make the scope, or the scope does not carry the limit, swap
# limit and OOM policy asked for (a manager without the memory controller
# delegated accepts MemoryMax and enforces nothing), this exits non-zero with
# the reason and COMMAND never starts. Never run it uncapped instead: that is
# the run that shuts the runner down.
#
# REPORT gets key=value lines, written only once the scope is verified:
# mechanism, unit, limit_bytes; then, after COMMAND exits, rc, oom_kill (the
# kernel's count for the scope) and peak_bytes. A REPORT without limit_bytes
# means COMMAND never ran. Exits with COMMAND's status.
set -uo pipefail
self=$(realpath "$0")
die() {
	echo "::error::memory cap: $*" >&2
	exit 2
}

inside() { # REPORT LIMIT UNIT MECHANISM -- COMMAND...
	local report=$1 limit=$2 unit=$3 mech=$4 cg rel got swap policy
	shift 5
	rel=$(sed -n 's/^0:://p' /proc/self/cgroup)
	cg=/sys/fs/cgroup$rel
	case "$rel" in */"$unit".scope) ;; *) die "this process is in cgroup '$rel', not in the scope $unit.scope" ;; esac
	got=$(cat "$cg/memory.max" 2>/dev/null) || die "$cg has no memory.max: the memory controller is not enabled for the scope, so no limit applies"
	[ "$got" = "$limit" ] || die "the scope's memory.max is '$got', not the $limit bytes asked for"
	if [ -e "$cg/memory.swap.max" ]; then
		swap=$(cat "$cg/memory.swap.max")
		[ "$swap" = 0 ] || die "the scope's memory.swap.max is '$swap', not 0"
	elif awk '$1 == "SwapTotal:" && $2 > 0 {found = 1} END {exit !found}' /proc/meminfo; then
		die "this host has swap and no swap accounting (no memory.swap.max): the scope could swap past its limit"
	fi
	if [ "$mech" = user ]; then policy=$(systemctl --user show -p OOMPolicy --value "$unit.scope")
	else policy=$(systemctl show -p OOMPolicy --value "$unit.scope"); fi
	[ "$policy" = continue ] || die "the scope's OOMPolicy is '$policy', not continue: the first OOM kill would stop the whole scope"
	printf 'mechanism=%s\nunit=%s.scope\nlimit_bytes=%s\n' "$mech" "$unit" "$limit" >"$report"
	echo "memory cap: $((limit / 1048576)) MiB, no swap, OOMPolicy=continue ($mech manager, $unit.scope)"
	"$@"
	local rc=$? kills peak
	kills=$(awk '$1 == "oom_kill" {print $2}' "$cg/memory.events")
	peak=$(cat "$cg/memory.peak" 2>/dev/null || echo unknown)
	printf 'rc=%s\noom_kill=%s\npeak_bytes=%s\n' "$rc" "${kills:-unknown}" "$peak" >>"$report"
	echo "memory cap: exit $rc, $kills OOM kill(s) in the scope, peak $([ "$peak" = unknown ] && echo unknown || echo "$((peak / 1048576)) MiB") of $((limit / 1048576)) MiB"
	return "$rc"
}

case "${1:-}" in
--inside)
	shift
	inside "$@"
	exit
	;;
--inside-env) # ENVFILE REPORT LIMIT UNIT MECHANISM -- COMMAND...: the caller's environment, exactly
	envfile=$2
	shift 2
	mapfile -d '' -t kv <"$envfile" || die "could not read the saved environment $envfile"
	rm -f "$envfile"
	exec env -i "${kv[@]}" bash "$self" --inside "$@"
	;;
esac

report=${1:?usage: memory_cap.sh REPORT -- COMMAND...}
[ "${2:-}" = -- ] || die "usage: memory_cap.sh REPORT -- COMMAND..."
shift 2
[ $# -gt 0 ] || die "usage: memory_cap.sh REPORT -- COMMAND..."
rm -f "$report"
command -v systemd-run >/dev/null || die "no systemd-run on this host"

page=$(getconf PAGESIZE)
if [ -n "${MUTMUT_MEMORY_MAX:-}" ]; then
	case "$MUTMUT_MEMORY_MAX" in
	*[!0-9KMG]* | [KMG]* | *[KMG]?*) die "MUTMUT_MEMORY_MAX='$MUTMUT_MEMORY_MAX' is not bytes or a number with a K, M or G suffix" ;;
	*K) limit=$((${MUTMUT_MEMORY_MAX%K} * 1024)) ;;
	*M) limit=$((${MUTMUT_MEMORY_MAX%M} * 1048576)) ;;
	*G) limit=$((${MUTMUT_MEMORY_MAX%G} * 1073741824)) ;;
	*) limit=$MUTMUT_MEMORY_MAX ;;
	esac
else
	total=$(awk '$1 == "MemTotal:" {print $2 * 1024}' /proc/meminfo)
	limit=$((total - 4 * 1073741824))
	[ "$limit" -ge 1073741824 ] || die "MemTotal is $((total / 1048576)) MiB: less 4 GiB for the runner leaves under 1 GiB for the scope. Set MUTMUT_MEMORY_MAX."
fi
limit=$((limit / page * page)) # the kernel keeps memory.max in whole pages
[ "$limit" -gt 0 ] || die "a limit of $limit bytes"
unit=memory-cap-$$-$(date +%s)
props=(-p MemoryMax="$limit" -p MemorySwapMax=0 -p OOMPolicy=continue)

if user_err=$(systemctl --user show-environment 2>&1 >/dev/null); then
	exec systemd-run --user --scope --quiet --collect --unit="$unit" "${props[@]}" \
		-- bash "$self" --inside "$report" "$limit" "$unit" user -- "$@"
fi
sudo_err=$(sudo -n true 2>&1) || die "no way to make the scope: no systemd user manager ($user_err) and no passwordless sudo for the system manager ($sudo_err)"
envfile=$(umask 077 && mktemp -p "${RUNNER_TEMP:-${TMPDIR:-/tmp}}") || die "could not write the environment for the scope"
env -0 >"$envfile"
exec sudo -n systemd-run --scope --quiet --collect --unit="$unit" "${props[@]}" \
	-- setpriv --reuid="$(id -u)" --regid="$(id -g)" --init-groups \
	-- bash "$self" --inside-env "$envfile" "$report" "$limit" "$unit" system -- "$@"
