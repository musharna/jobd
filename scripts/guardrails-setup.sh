#!/usr/bin/env bash
# Repo-specific setup for the template-owned workflows (guardrails.yml and
# nightly-guardrails.yml run this, when it exists, before they install).
#
# Those two workflows are the same file in every repo, so they cannot pass this
# repo's PRIVACY_FORBIDDEN secret, and tests/test_no_private_paths.py fails
# without its list. It skips instead on a run the runner itself names as one of
# the two. The "tests with no network" step re-runs the suite through sudo,
# which drops GITHUB_* and keeps HOME, so the same fact is left here as a file.
# ci.yml never runs this script: its test jobs carry the scan and a missing
# list still fails there.
set -euo pipefail

case "${GITHUB_ACTIONS:-}:${GITHUB_WORKFLOW:-}" in
true:guardrails | true:nightly-guardrails)
	mkdir -p "$HOME/.config/jobd"
	printf '%s\n' "$GITHUB_WORKFLOW" >"$HOME/.config/jobd/privacy-no-list"
	echo "guardrails-setup: no privacy list in the '$GITHUB_WORKFLOW' workflow (marker written)"
	;;
*)
	echo "guardrails-setup: not a template-owned workflow run, nothing to do"
	;;
esac
