#!/usr/bin/env bash
# The nightly's memory cap (scripts/memory_cap.sh) on this host's real systemd:
# a process that allocates without end is killed and the rest of the scope runs
# on; a cap that is not in place stops the command before it starts; and the
# nightly's run step, as written, survives a planted runaway mutant and names it.
#
#   bash scripts/memory-cap-selftest.sh [workflow.yml]   (default: this repo's nightly)
#
# Needs a host that can make the scope: a systemd user manager, or passwordless
# sudo (a GitHub-hosted runner). Under a minute. KEEP=1 keeps the scratch dir.
# Every allocation here is bounded twice: by a scope's limit, and by the
# allocating process's own ceiling, so a broken cap fails a check instead of
# taking the host's memory.
set -uo pipefail
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR
here=$(cd "$(dirname "$0")" && pwd)
if [ -n "${1:-}" ]; then wf=$(realpath "$1")
elif [ -f "$here/../templates/workflows/nightly-guardrails.yml" ]; then wf=$here/../templates/workflows/nightly-guardrails.yml
else wf=$here/../.github/workflows/nightly-guardrails.yml; fi
cap=$here/memory_cap.sh
t=$(mktemp -d)
if [ "${KEEP:-0}" = 1 ]; then echo "scratch: $t"; else trap 'rm -rf "$t"' EXIT; fi
n=0
f=0
want() { # name want got
	n=$((n + 1))
	if [ "$2" = "$3" ]; then echo "ok   $1 ($3)"; else
		echo "FAIL $1: want '$2', got '$3'"
		f=$((f + 1))
	fi
}
kv() { sed -n "s/^$2=//p" "$1" 2>/dev/null | tail -1; } # report key

# --- a hog and its sibling, in one scope ----------------------------------------
# The hog allocates 1 MiB at a time up to 1 GiB (exit 3: never stopped); the
# sibling holds 16 MiB and waits for the hog to end (exit 0), or 30 s (exit 4).
# The shell also says whether it runs as the caller, with the caller's
# environment: under sudo the cap starts it again as this user.
cat >"$t/pair.sh" <<'EOF'
cd "$1" || exit 9
if [ "$MEMCAP_MARK" = "$(printf 'two words\nand a line')" ] && [ "$(id -u):$(id -G)" = "$MEMCAP_ID" ]; then me=same; else me=changed; fi
python3 -c 'b = [bytearray(1 << 20) for _ in range(1024)]; raise SystemExit(3)' &
h=$!
python3 -c '
import os, time
x = bytearray(16 << 20)
for _ in range(600):
    if os.path.exists("hog.done"):
        raise SystemExit(0)
    time.sleep(0.05)
raise SystemExit(4)' &
s=$!
wait "$h"
hog=$?
touch hog.done
wait "$s"
echo "hog=$hog sibling=$? shell=alive caller=$me" >pair.out
EOF
pair() { # cap-script dir: the pair under a 256 MiB cap -> "<cap exit> <pair.out> oom_kill=<n>"
	mkdir -p "$2"
	# 262145K is a quarter page over 256 MiB: the cap rounds it down to whole pages.
	MEMCAP_MARK=$(printf 'two words\nand a line') MEMCAP_ID="$(id -u):$(id -G)" MUTMUT_MEMORY_MAX=262145K \
		timeout 120 bash "$1" "$2/report" -- bash "$t/pair.sh" "$2" >"$2/log" 2>&1
	echo "$? $(cat "$2/pair.out" 2>/dev/null || echo 'no pair.out') oom_kill=$(kv "$2/report" oom_kill)"
}
want "the hog is killed for memory; its sibling and the shell run on and the command exits 0" \
	"0 hog=137 sibling=0 shell=alive caller=same oom_kill=1" "$(pair "$cap" "$t/a")"
want "  ... the report has the limit asked for, in whole pages" 268435456 "$(kv "$t/a/report" limit_bytes)"
echo "     (made by the $(kv "$t/a/report" mechanism) manager)"

# --- a cap that is not in place: the command never starts -----------------------
# Each of these makes the same call as the case above, with one thing wrong.
# `ran` is the command's own mark: the real cap's run makes it (the positive
# control); a cap that is not in place must not.
mark() { # dir [env...]: the cap around `touch ran` -> "<exit> <ran|not run> <report|no report>"
	local d=$1
	shift
	mkdir -p "$d"
	(cd "$d" && env "$@" bash "$cap" "$d/report" -- touch "$d/ran" >"$d/log" 2>&1)
	echo "$? $([ -e "$d/ran" ] && echo ran || echo 'not run') $([ -e "$d/report" ] && echo report || echo 'no report')"
}
want "control: the real cap runs the command" "0 ran report" "$(mark "$t/m0" MUTMUT_MEMORY_MAX=64M)"
mkdir -p "$t/m5"
MUTMUT_MEMORY_MAX=64M bash "$cap" "$t/m5/report" -- bash -c 'exit 124' >"$t/m5/log" 2>&1
want "the command's exit status comes back (124: the nightly's wall-clock cap)" "124 rc=124" "$? rc=$(kv "$t/m5/report" rc)"
want "a limit that does not parse stops the command" "2 not run no report" "$(mark "$t/m1" MUTMUT_MEMORY_MAX=64MB)"
want "  ... and says why" 1 "$(grep -c "MUTMUT_MEMORY_MAX='64MB' is not" "$t/m1/log")"
# A manager that takes MemoryMax and makes no scope (memory controller not
# delegated, or no systemd at all): systemd-run, sudo and setpriv run the
# command as given. The checks inside the scope are the real ones.
mkdir -p "$t/noscope" "$t/nomanager"
cat >"$t/noscope/systemd-run" <<'EOF'
#!/usr/bin/env bash
while [ "$1" != -- ]; do shift; done
shift
exec "$@"
EOF
cp "$t/noscope/systemd-run" "$t/noscope/setpriv"
printf '#!/usr/bin/env bash\n[ "$1" = -n ] && shift\n[ "$1" = true ] && exit 0\nexec "$@"\n' >"$t/noscope/sudo"
chmod +x "$t/noscope/"*
want "a scope that is not made stops the command" "2 not run no report" "$(mark "$t/m2" MUTMUT_MEMORY_MAX=64M PATH="$t/noscope:$PATH")"
want "  ... and says why" 1 "$(grep -c "not in the scope memory-cap-" "$t/m2/log")"
# No user manager and no sudo.
printf '#!/usr/bin/env bash\necho "Failed to connect to bus: No medium found" >&2\nexit 1\n' >"$t/nomanager/systemctl"
printf '#!/usr/bin/env bash\necho "sudo: a password is required" >&2\nexit 1\n' >"$t/nomanager/sudo"
chmod +x "$t/nomanager/"*
# A manager that drops the limit (a user manager without the memory
# controller delegated takes MemoryMax and enforces nothing): the real
# systemd-run, minus MemoryMax.
mkdir -p "$t/nolimit"
printf '#!/usr/bin/env bash\nargs=()\nfor a in "$@"; do case $a in MemoryMax=*) unset "args[-1]" ;; *) args+=("$a") ;; esac; done\nexec %q "${args[@]}"\n' \
	"$(command -v systemd-run)" >"$t/nolimit/systemd-run"
if command -v sudo >/dev/null; then
	printf '#!/usr/bin/env bash\n[ "$1" = -n ] && shift\nif [ "$1" = systemd-run ]; then shift; exec %q -n %q "$@"; fi\nexec %q -n "$@"\n' \
		"$(command -v sudo)" "$t/nolimit/systemd-run" "$(command -v sudo)" >"$t/nolimit/sudo"
fi
chmod +x "$t/nolimit/"*
want "a scope without the limit stops the command" "2 not run no report" "$(mark "$t/m4" MUTMUT_MEMORY_MAX=64M PATH="$t/nolimit:$PATH")"
want "  ... and says why" 1 "$(grep -c "memory.max is 'max', not the 67108864 bytes asked for" "$t/m4/log")"
want "no manager to make the scope stops the command" "2 not run no report" "$(mark "$t/m3" MUTMUT_MEMORY_MAX=64M PATH="$t/nomanager:$PATH")"
want "  ... and says why, in both tools' words" 1 "$(grep -c "no systemd user manager (Failed to connect to bus: No medium found) and no passwordless sudo for the system manager (sudo: a password is required)" "$t/m3/log")"

# --- the nightly's run step, as written, on real mutmut --------------------------
# drain() is jobd's stream_output loop in small: a read that returns b"" at EOF,
# a post for every pass, and a test fake that keeps a 1 MiB copy of each post.
# The mutant `if not chunk:` -> `if chunk:` never leaves the loop. The step runs
# inside a stand-in runner that does not survive running out of memory: a 1 GiB
# scope under systemd's default OOM policy, which stops the whole scope at the
# first OOM kill. The step's own cap (512 MiB) must kill the runaway first.
# PYTHON_CPU_COUNT: two mutmut workers on any host, so the memory the tree
# legitimately needs does not depend on the host's core count.
S=$t/steps
mkdir -p "$S"
(cd "$here" && uv run -q --no-project --with pyyaml python - "$wf" "$S") <<'PY' || exit 1
import pathlib, shlex, sys
import yaml
wf, out = sys.argv[1], pathlib.Path(sys.argv[2])
job = yaml.safe_load(open(wf))["jobs"]["mutation"]
(out / "job.env").write_text("".join(f"{k}={shlex.quote(str(v))}\n" for k, v in job["env"].items()))
for key, prefix in [("install", "Install + mutmut"), ("run", "Run mutants")]:
    hit = [s for s in job["steps"] if s.get("name", "").startswith(prefix)]
    if not hit:
        sys.exit(f"no step named {prefix!r} in {wf}")
    env = hit[0].get("env") or {}
    if any("${{" in str(v) for v in env.values()):
        sys.exit(f"step {key}: an env expression this test cannot emulate: {env}")
    (out / f"{key}.env").write_text("".join(f"{k}={shlex.quote(str(v))}\n" for k, v in env.items()))
    (out / f"{key}.run").write_text(hit[0]["run"])
PY
runner=$t/runner_cap.sh
python3 - "$cap" "$runner" <<'PY' || exit 1
import sys
s = open(sys.argv[1]).read()
for a, b in [("-p OOMPolicy=continue", "-p OOMPolicy=stop"), ('[ "$policy" = continue ]', '[ "$policy" = stop ]')]:
    if s.count(a) != 1:
        sys.exit(f"memory_cap.sh: {a!r} occurs {s.count(a)} times, not once: rework the stand-in runner")
    s = s.replace(a, b)
open(sys.argv[2], "w").write(s)
PY
r=$t/repo
mkdir -p "$r/src/pkg" "$r/tests" "$r/scripts" "$r/.rt"
cp "$here/uv_extras.py" "$cap" "$r/scripts/"
cat >"$r/pyproject.toml" <<'EOF'
[project]
name = "pkg"
version = "0.1.0"
requires-python = ">=3.13"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/pkg"]

[dependency-groups]
dev = ["pytest"]

[tool.mutmut]
source_paths = ["src"]
EOF
cat >"$r/src/pkg/__init__.py" <<'EOF'
def drain(read, post):
    passes = 0
    while True:
        chunk = read()
        post(chunk)
        passes += 1
        if not chunk:
            break
    return passes


def sign(x):
    if x < 0:
        return -1
    return 1
EOF
cat >"$r/tests/test_pkg.py" <<'EOF'
from pkg import drain, sign


def test_drain_stops_at_eof():
    posts = []
    assert drain(lambda: b"", lambda chunk: posts.append(bytearray(1 << 20))) == 1
    assert len(posts) == 1


def test_sign():
    assert sign(-5) == -1
EOF
step() { # sid [runner-cap]: one step as the runner runs it, its log in .rt/log.<sid>
	(
		cd "$r" || exit 9
		set -a
		export RUNNER_TEMP=$r/.rt GITHUB_OUTPUT=$r/.rt/out.$1 GITHUB_STEP_SUMMARY=$r/.rt/summary GITHUB_ENV=$r/.rt/env
		touch "$GITHUB_ENV"
		# shellcheck disable=SC1090,SC1091  # generated from the workflow above / written by earlier steps
		. "$S/job.env" && . "$GITHUB_ENV" && . "$S/$1.env"
		set +a
		if [ -n "${2:-}" ]; then
			MUTMUT_MEMORY_MAX=1G timeout 600 bash "$2" "$r/.rt/runner" -- \
				env MUTMUT_MEMORY_MAX=512M PYTHON_CPU_COUNT=2 bash --noprofile --norc -eo pipefail "$S/$1.run"
		else
			timeout 600 bash --noprofile --norc -eo pipefail "$S/$1.run"
		fi
	) >"$r/.rt/log.$1" 2>&1
}
step install || {
	echo "FAIL the install step exited $?"
	tail -20 "$r/.rt/log.install"
	exit 1
}
step run "$runner"
rc=$?
statuses=$(cd "$r" && uv run -q mutmut results --all true 2>/dev/null | sed 's/^ *//')
of() { grep -c ": $1$" <<<"$statuses"; }
runaway=$(python3 - "$r" <<'PY'
import json, pathlib, sys
print(" ".join(sorted(k for m in pathlib.Path(sys.argv[1], "mutants").rglob("*.meta")
                      for k, c in json.loads(m.read_text())["exit_code_by_key"].items() if c == -9)))
PY
)
diff=$( [ -n "$runaway" ] && cd "$r" && uv run -q mutmut show "$runaway" 2>/dev/null)
want "the run step finishes in a runner that cannot survive running out of memory, and mutmut exits 0" \
	"0 mutmut exit: 0" "$rc $(grep -o 'mutmut exit: [0-9]*' "$r/.rt/log.run" | tail -1)"
want "  ... the kernel killed one process in the step's scope, and the runner was never short" \
	"oom_kill=1 runner oom_kill=0" "oom_kill=$(kv "$r/.rt/memory-cap.txt" oom_kill) runner oom_kill=$(kv "$r/.rt/runner" oom_kill)"
want "  ... one mutant is recorded killed (exit -9), and it is the one that never leaves the loop" \
	"1 +        if chunk:" "$(wc -w <<<"$runaway") $(grep -m1 -E '^\+ +if chunk:' <<<"$diff")"
want "  ... the step's log names it" 1 "$(grep -cxF "  killed: $runaway" "$r/.rt/log.run")"
want "  ... so does the job summary" 1 "$(grep -cxF -- "- \`$runaway\`" "$r/.rt/summary" 2>/dev/null)"
# The positive control: every other mutant got its verdict as on any night.
want "  ... every other mutant was checked: kills and survivors as planted" \
	"0 not checked, kills: yes, survivors: yes" \
	"$(of 'not checked') not checked, kills: $([ "$(of killed)" -gt 0 ] && echo yes || echo no), survivors: $([ "$(of survived)" -gt 0 ] && echo yes || echo no)"

echo "$((n - f)) of $n checks passed"
if [ "$f" -gt 0 ]; then
	echo "--- the run step's log (last 30 lines)"
	tail -30 "$r/.rt/log.run"
	exit 1
fi
