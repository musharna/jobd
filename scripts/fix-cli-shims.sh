#!/usr/bin/env bash
# fix-cli-shims.sh — repair jobd CLI entry points that a stray `pip install jobd`
# left in an env EARLIER on PATH than the canonical venv.
#
# Failure mode this fixes: `job ...` (or jobd / jobd-mcp / jobd-worker) throws
#   ModuleNotFoundError: No module named 'job_cli'
# because a shim like ~/miniconda3/bin/job has a shebang pointing at a base
# interpreter that does NOT have job_cli installed, and that dir precedes the real
# venv (~/jobd/.venv/bin) on PATH — so the broken shim wins.
#
# Fix: back up each shadowing broken shim and symlink it to the canonical venv
# entry point. Idempotent; safe to re-run.
set -euo pipefail

VENV_BIN="${JOBD_VENV_BIN:-$HOME/jobd/.venv/bin}"
CMDS=(job jobd jobd-mcp jobd-worker)

if [[ ! -x "$VENV_BIN/job" ]]; then
        echo "ERROR: canonical jobd venv not found at $VENV_BIN (set JOBD_VENV_BIN)." >&2
        exit 1
fi

fixed=0
for c in "${CMDS[@]}"; do
        # the shim that wins on PATH, if it isn't already the canonical venv one
        shim="$(command -v "$c" 2>/dev/null || true)"
        [[ -z "$shim" ]] && continue
        canon="$VENV_BIN/$c"
        # already correct (same file or a symlink to the venv)?
        if [[ "$shim" -ef "$canon" ]]; then continue; fi
        # does the shim actually work? (imports job_cli) — if so, leave it
        if "$shim" --help >/dev/null 2>&1; then continue; fi
        echo "repairing broken shim: $shim -> $canon"
        mv -f "$shim" "$shim.broken-bak"
        ln -sf "$canon" "$shim"
        fixed=$((fixed + 1))
done

hash -r 2>/dev/null || true
echo "done: repaired $fixed shim(s). 'job' now resolves to: $(command -v job)"
