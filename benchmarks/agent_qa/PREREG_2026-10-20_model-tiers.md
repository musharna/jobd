# Pre-registration: model-tier re-run, 2026-10-20

Committed before the confirmatory sweeps. Anything run after them is
exploratory and is labelled so.

## Why

On Opus 5.5 (10-18) both arms were at the ceiling: 38 vs 37 of 38 questions
right in all runs, a tie. The question now is whether jobd-mcp lifts a weaker
model's accuracy over the same model with the `job` CLI.

## Tiers

- **Haiku 5.5** (`claude-haiku-5-5`), then **Sonnet 5.5**
  (`claude-sonnet-5-5`) in the same session, right after Haiku, with the same
  design. Each tier is a separate confirmatory sweep.
- `run.py --model` checks that the init event names the model asked for, and
  stops the sweep otherwise. Every row records the served model, and
  `versions.json` records the model asked for.

## Server and CLI under test

- mcp arm: `jobd[mcp]==0.5.52` from PyPI, the current release, through `uvx`.
  Its MCP source is identical to commit `5475cab`, which the 10-18 re-run ran
  from git (`git diff 5475cab v0.5.52 -- src/jobd/mcp` is empty).
- alt arm: `jobd==0.5.52` from PyPI (the 10-18 alt arm ran 0.5.51). Between
  v0.5.51 and v0.5.52 the only change under `src/` outside the MCP server is
  an `offset` argument in `client.py`, which the CLI does not use.
- `--exclude-newer 2026-10-20T06:00:00Z` for both arms, just after 0.5.52's
  upload (05:32 UTC). The 10-18 date would have excluded 0.5.52 itself.
- Claude Code 2.1.305 (10-18: 2.1.302).

## Key: the 10-18 key, unchanged

`~/jobd-artifacts/agent_qa/rerun-2026-10-18/questions.json`, sha256
`30216477fb9061293bb269325f670efcfa1c46c63fa50e8aca55fc845254a756`, 38
questions. It was not rebuilt. Checks on 2026-10-20 at about 22:40 UTC:

- **Oracle:** 38 of 38 key answers score right, and 38 of 38 near misses score
  wrong.
- **Drift:** `build_hard.drift` recomputed every answer from the live broker,
  and all 38 are unchanged. Every window is closed, and the latest ends at
  2026-10-18T07:00Z, before the freeze. Event and log questions read the same
  events and log tails.
- **What moved:**
  - The broker went from 0.5.51 to 0.5.52, but no broker code changed (see
    above).
  - 106 jobs were submitted after the freeze, and 98 finished after it. All 98
    finished after every window, so no answer counts them. They only add
    pages, newest first, in front of the windows, in both arms alike.
  - The fleet snapshot is unchanged: desktop, gt76, laptop and msi-4080, all
    online.
- `run.py` refuses a broker whose version differs from the key's. The new
  `--accept-broker 0.5.52` accepts exactly that version, and then only if the
  drift check finds nothing. `versions.json` records both versions.

## Arms, reps, order

- `bare` (no tools), `alt` (Bash with the `job` CLI behind `job_guard.py`, and
  the same read-only text tools) and `mcp` (jobd-mcp's six read tools).
- The read-only proxy, the `job` guard, the empty `ZDOTDIR` and the system
  prompt are exactly as on 10-18. Each run has its own TMPDIR.
- All three arms are re-run on each tier. No Opus row is reused in a primary
  test.
- 3 reps, in rep, then question, then arm order (bare, alt, mcp), so the arms
  are interleaved by question.
- `--workers 2`, under
  `flock …/sweep.lock env HEAVY_RUN_MEM=6G heavy-run`. Each run waits on the
  model API and the broker, not on local CPU. Every run still has its own
  proxy, TMPDIR and working directory.
- Output goes to `~/jobd-artifacts/agent_qa/tiers-2026-10-20/<model>/`:
  `results.jsonl`, `versions.json`, `pauses.jsonl`, and the transcripts in
  `runs-<model>/`.

## Pilot, then the sweeps, in one lock hold

The run order is fixed now, before any pilot row exists. One script
(`~/jobd-artifacts/agent_qa/tiers-2026-10-20/scripts/sweep.sh`) runs the
steps in order.

1. **Pilot.** Haiku, 1 rep of h02, h12, h22, h29 and h35, all three arms (15
   runs), kept apart in `pilot-claude-haiku-5-5/`. It checks feasibility
   only.
2. **Gate.** `pilot_gate.py` stops the script unless:
   - every pilot run exited 0 with a result;
   - every run was served by Haiku;
   - no run had an API failure;
   - each tool arm made a call;
   - the newest usage reading is under the start limits.

   The gate reads no verdicts, and no pilot row enters the analysis.
3. **Haiku sweep**, then its API-failure re-run.
4. **Sonnet sweep**, then its API-failure re-run.

## Primary endpoint, per tier

Questions right in all 3 runs, mcp against alt, out of 38. An invalid run
counts as not right. The validity rule is the 10-16 one: exit status, the
tools offered, at least one tool call, and the leak scan.

- The test is an exact two-sided McNemar test on the discordant questions.
- The CI is a bootstrap 95% CI of (mcp − alt) / 38: 10,000 resamples of
  questions, seed 20261016.
- Both are computed by `analyze.win_rule`, the same as on 10-18.

## Win rule (per server, across both tiers)

- Holm-adjust the two tiers' McNemar p values (`tiers.py`).
- A tier shows a **lift** if its Holm p is below 0.05, more questions only mcp
  got right, and the CI lies wholly above 0.
- A tier shows a **loss** if its Holm p is below 0.05, more questions only alt
  got right, and the CI lies wholly below 0.
- **WIN** = a lift at one or more tiers and no loss at the other.
  **LOSS** = a loss and no lift. **MIXED** = both. Anything else is a **TIE**.
- If Sonnet is stopped by the usage gate, Haiku is reported with its raw p,
  and the verdict stays pending.

## Secondary (reported, not deciding)

- **Substitute claim:** each tier's mcp arm against the 10-18 Opus alt arm
  (`rerun-2026-10-18/scored.jsonl`), with the same McNemar test and CI
  (`analyze.py --substitute`). It is non-concurrent: two days apart, and the
  CLI, broker and Claude Code versions differ (see above).
- **Floors:** the bare arm and the two no-model baselines (prior and best
  constant).
- **Mechanism:**
  - server tool calls per mcp run;
  - mcp runs with no server call (invalid under the validity rule, and
    reported as "mechanism did not fire");
  - runs that passed `finished_after` / `finished_before` (`diagnose.py`).
- **Cost and time:** cost and seconds per answer, with the 10-18 analysis.

## Usage limits and harness failures

- **Start condition:** start only if the newest transcript's five-hour
  utilization is below 0.85 and its seven-day utilization is below 0.90.
- **During the sweep:** `run.py` pauses until `resetsAt` whenever a transcript
  shows five-hour utilization of 0.85 or more, or a `rejected` status, and
  records each pause in `pauses.jsonl`.
- **Stop:** the sweep stops at seven-day utilization of 0.90 or more
  (`--stop-at 0.90`, per the 10-20 amendment to the brief; the default stays
  0.80). Rows already written are kept, and the stop is reported.
- **Harness failures:** a row that the API ended before the model answered is
  a harness failure. It is set aside with `--drop-api-failures` and re-run
  once. If it fails again, it counts as invalid. Timeouts are not re-run.
- An interrupted sweep is resumed, never restarted, and never stopped early on
  interim results.

## Analysis

```bash
K=~/jobd-artifacts/agent_qa/rerun-2026-10-18/questions.json
for M in claude-haiku-5-5 claude-sonnet-5-5; do
  T=~/jobd-artifacts/agent_qa/tiers-2026-10-20/$M
  uv run python benchmarks/agent_qa/analyze.py --key $K --runs $T/runs-$M \
    --results $T/results.jsonl --outdir benchmarks/agent_qa/tiers_2026-10-20/$M \
    --substitute ~/jobd-artifacts/agent_qa/rerun-2026-10-18/scored.jsonl
  uv run python benchmarks/agent_qa/diagnose.py $T/runs-$M
done
uv run python benchmarks/agent_qa/tiers.py --key $K \
  --tier haiku=~/jobd-artifacts/agent_qa/tiers-2026-10-20/claude-haiku-5-5/scored.jsonl \
  --tier sonnet=~/jobd-artifacts/agent_qa/tiers-2026-10-20/claude-sonnet-5-5/scored.jsonl
```

Every result is reported, whatever it shows.

## Deviations (appended after the sweeps started)

- **Sonnet paused at 52 of 342 runs** (rep 1, through h17/h18), 22:58 EDT on
  10-20, on the coordinator's order: seven_day was 0.84–0.85, and four other
  servers had no Haiku rows yet. Haiku runs first everywhere, and Sonnet
  resumes later with whatever budget is left. The stop at 0.90 is unchanged.
  The runs in flight were killed before they wrote a row. `run.py` resumes by
  skipping finished (arm, question, rep) triples, so those runs are redone
  from scratch. The 52 rows are kept. No result was looked at before the
  pause.
- **Mechanism:** `diagnose.py` stops at its price fit on Haiku transcripts,
  because their costs are not linear in its four token kinds. So the
  mechanism readout uses its `run_row` without the fit
  (`tiers-2026-10-20/scripts/mechanism.py`). The cost breakdown by token kind
  is not reported.

## Amendment, 2026-10-23 ~19:20 EDT: API billing (before any further run)

- **Billing.** The user's decision: every remaining run in this design (both
  tiers) bills an Anthropic API key, not the Claude subscription. Same model
  IDs, prompts, arms, reps, harness and controls. Claude Code is 2.1.309 now
  (recorded per row).
- **How.** A benchmark-only `claude` wrapper, first on `PATH` for the sweep,
  loads the key and then `exec`s the real CLI (so the harness's timeout still
  kills Claude itself). It records each run's cost (Claude Code's
  `total_cost_usd`; for a killed run, estimated from its token counts) and
  refuses to start a run once the total reaches $171 of the user's $175 cap
  (exit 75, no transcript). A run whose `init` event does not show
  `apiKeySource: ANTHROPIC_API_KEY` stops every later launch.
- **Gate.** API calls report no subscription windows, so the seven-day stop
  and the five-hour pause no longer fire. `run.py` instead asks the spend gate
  (`BENCH_SPEND_GATE`) before every run and, on a refusal, stops cleanly as
  at the seven-day stop: finished rows stay, nothing new starts. Test added,
  seen to fail with the check disabled (`mutate-run`).
- **Set aside by exit code, not outcome.** A run the wrapper refuses (exit
  75) is a harness failure, not a result: it is set aside and re-run once.
- **Concurrency.** Up to 4 servers' sweeps run at once (2 before). The
  workers per sweep are unchanged.
