# Agent benchmark: questions about a live broker, with and without jobd-mcp

**Latest (2026-10-18, harder questions):** on 38 questions about time windows,
totals over many jobs, the event record and logs, Claude Opus 5.5 got 38 right
in all 3 runs with jobd-mcp and 37 with the `job` CLI. That is a tie under the
pre-registered rule (McNemar p = 1): accuracy still does not separate them.
With the server, the median answer cost $0.032 against $0.164 (ratio 0.20, 95%
CI 0.15 to 0.28) and took 8.8 s against 53 s (0.16, 0.13 to 0.20), and it was
cheaper and faster on every one of the 38 questions. Cost and time were
secondary endpoints. The server tested is an unreleased commit. See
[Re-run 2026-10-18](#re-run-2026-10-18-server-5475cab).

**First sweep (2026-10-16):** we asked 41 easier read-only questions, 3 times
each, with no tools, with the `job` CLI, and with jobd-mcp 0.5.51. Both tool
setups got every question right in every run (exact McNemar test, 0
discordant questions, p = 1). With the server the median answer took 6.7 s
against 13.6 s, a ratio of 0.49 (95% CI 0.44 to 0.59), and used 1.1 fewer tool
calls on average (0.8 to 1.5). Its median cost was lower, a ratio of 0.76
(0.67 to 0.81), but its mean cost was the same, 1.00 (0.78 to 1.31), because
one question cost 2 to 3 times as much with the server.

![Right, wrong or declined per setup](agent_qa.png)

## Results

| Setup | Right | Wrong | Declined | Invalid | Questions right in all 3 runs | Cost / answer, median (mean) | Seconds / answer, median (mean) | Tool calls / answer, median (mean) |
|---|---|---|---|---|---|---|---|---|
| Constant guess, no model (1 answer per question) | 4 | 37 | 0 | 0 | 4 of 41 | – | – | – |
| Best constant, fit to the key, no model (1 per question) | 10 | 31 | 0 | 0 | 10 of 41 | – | – | – |
| Claude alone | 0 | 0 | 123 | 0 | 0 of 41 | $0.009 ($0.010) | 4.8 (5.4) | 0 (0) |
| Claude + job CLI | 123 | 0 | 0 | 0 | 41 of 41 | $0.028 ($0.038) | 13.6 (16.4) | 2 (2.9) |
| Claude + jobd-mcp | 123 | 0 | 0 | 0 | 41 of 41 | $0.022 ($0.038) | 6.7 (8.2) | 1 (1.8) |

Counts are out of 123 answers per Claude setup and 41 per baseline. A
question's three runs are not independent, so the questions column is the
fairer count. The table comes from [`summary.tsv`](summary.tsv) and the tests
from [`stats.tsv`](stats.tsv). Intervals are 95% bootstrap intervals over the
41 questions (10,000 draws, seed 20261016), resampling a question's three runs
together.

Per question, on the median of its three runs, the server against the CLI:

- Time: 6.7 s (6.1 to 7.7) against 13.6 s (11.2 to 15.8). The server was
  faster on all 41 questions (sign test p = 9e-13).
- Tool calls: fewer on 37 questions, more on 3, 1 tie (p = 2e-8). Every one
  of the 123 CLI runs started by reading `job --help`.
- Cost: cheaper on 35 questions, more expensive on 6 (p = 5e-6). Total cost
  was $4.66 for the server's 123 answers and $4.65 for the CLI's.

Mean cost is equal while median cost is not because of q26, which asks for
every failed job of one project on one day. It cost $0.33 to $0.46 per run
with the server and $0.13 to $0.17 with the CLI. `jobd_list` rows carry no
`finished_at` and the tool has no date filter, so Claude fetched all 194 of
the project's failures (41,808 characters as Claude received them), then
`job_completed` events since a chosen date (up to 47,795 characters) to find
the day. With the CLI it ran `job status` for each job and grepped the
`finished` line.

The
server's 5 `jobd_submit` calls were all dry runs, for routing questions; the
CLI has no dry run, so it answered those from the tags `job workers` prints.
The slowest single answers took 18 s (alone), 55 s (CLI) and 26 s (server).

![Cost and time per answer, every run](agent_qa_cost_time.png)

![Right, wrong or declined by kind of question](agent_qa_by_category.png)

## Re-run 2026-10-18 (server 5475cab)

Pre-registered in [`PREREG_2026-10-18.md`](PREREG_2026-10-18.md) before the
sweep. The results are in [`rerun_2026-10-18/`](rerun_2026-10-18/).

**Why.** [`diagnose.py`](diagnose.py) on the 10-16 transcripts found that the
server's tool definitions cost little: they were mostly read from cache. One
kind of question cost a lot. The 18 "failed jobs of a project on a day" runs
were 45% of the server's spend, at a median of 6.5 turns and 11,000 characters
of tool output, because `jobd_list` had no date filter and no `finished_at`.

**What changed (MCP side only, no broker change).** `jobd_list` takes
`finished_after` / `finished_before` and pages through the broker's existing
`limit`/`offset`. Its rows carry `finished_at` and `duration_s`. The server's
instructions say each tool is listed under its `mcp__jobd__` prefix. The mcp
arm ran commit `5475cab` from git (`--server-spec`), not a PyPI release. The
CLI arm stayed on `jobd==0.5.51`.

**Questions.** [`build_hard.py`](build_hard.py) drew 38 questions from the
uses the README and agent cookbook describe: counts and failed-job lists in a
UTC window, the busiest host or project in a window, summed run time,
`sweep_warning` counts and queue wait from the event record, and which of a
window's failed jobs has a given last log line. It reads the broker's HTTP API
without importing `jobd`. The key is frozen outside the repo. The oracle
passed (38 of 38 right, 38 of 38 near misses wrong), and before the sweep the
broker still gave every answer.

| Setup | Right | Wrong | Questions right in all 3 runs | Cost / answer, median (mean) | Seconds / answer, median | Tool calls / answer, mean |
|---|---|---|---|---|---|---|
| Constant guess, no model | 3 | 35 | 3 of 38 | – | – | – |
| Best constant, fit to the key | 9 | 29 | 9 of 38 | – | – | – |
| Claude + job CLI | 113 | 1 | 37 of 38 | $0.164 ($0.162) | 53.2 | 12.3 |
| Claude + jobd-mcp | 114 | 0 | 38 of 38 | $0.032 ($0.038) | 8.8 | 1.4 |

- **Win rule: TIE.** Non-inferior (38 ≥ 37 − 2), but not better: 1
  discordant question (only mcp right), McNemar p = 1, CI of the difference
  in share right 0.00 to +0.08. The CLI's one miss left job 4147 out of a list
  of six failures.
- **Cost and time (secondary):** cheaper and faster on all 38 questions
  (sign tests p = 7e-12). The sweep cost $22.75: $18.48 for the CLI arm and
  $4.28 for the server arm. The CLI arm answered window questions by running
  `job status` for every job `job list` returned, because `job list` prints no
  times.
- **Mechanism:** 96 of 114 server runs passed `finished_after` /
  `finished_before`, every run of a window or log-join question. No call was
  refused for a bare tool name. The most expensive server answer (h22, $0.21)
  received all 160 jobs of a window to tally by project; the server has no
  group-by.
- **Controls:** 228 runs, 0 invalid, 0 leak-scan hits, 0 proxy refusals, 0
  usage pauses, 0 API failures.

![Cost and time per answer, 2026-10-18](rerun_2026-10-18/agent_qa_cost_time.png)

![By kind of question, 2026-10-18](rerun_2026-10-18/agent_qa_by_category.png)

## Haiku and Sonnet re-run 2026-10-20

Pre-registered in
[`PREREG_2026-10-20_model-tiers.md`](PREREG_2026-10-20_model-tiers.md).
Same 38 questions and key as on 10-18; the oracle passed and no answer
drifted. Server `jobd[mcp]==0.5.52`, CLI `jobd==0.5.52`, all three arms
re-run on each tier.

**Win rule across both tiers: WIN.** Holm-adjusted McNemar p: Haiku 0.0078
(lift: 9 questions only the server got right in all runs, 0 only the CLI; CI
+0.11 to +0.37), Sonnet 0.11 (8 against 2, CI 0.00 to +0.32: neither a lift
nor a loss). [`tiers.py`](tiers.py) prints the verdict.

### Haiku 5.5

Results are in [`tiers_2026-10-20/claude-haiku-5-5/`](tiers_2026-10-20/claude-haiku-5-5/).

| Setup (Haiku 5.5) | Right / 114 | Questions right in all 3 runs | Cost / answer, median | Seconds / answer, median |
|---|---|---|---|---|
| No tools | 0 | 0 of 38 | – | – |
| + job CLI | 105 | 29 of 38 | $0.0057 | 69.5 |
| + jobd-mcp | 114 | 38 of 38 | $0.0009 | 9.8 |

- **Primary (raw p):** 9 questions only the server got right in all runs, 0
  only the CLI. McNemar p = 0.0039, and the CI of the difference in share
  right is +0.11 to +0.37. Of the CLI's 9 misses, 7 were runs that gave up
  (`UNKNOWN`), 1 was a wrong total run time, and 2 were right answers voided
  by the leak scan (see below). If those 2 count as right, it is 38 vs 31,
  p = 0.016.
- **Mechanism:** every server run called the server, 1.4 times on average.
  96 of 114 passed `finished_after` / `finished_before`.
- **Substitute (secondary, non-concurrent):** Haiku with the server got 38 of
  38 against Opus with the CLI's 37 of 38 on 10-18 (p = 1).
- **Cost:** the sweep cost $1.87: $0.99 for no tools (6 runs spent 5 to 8
  minutes writing without an answer), $0.76 for the CLI and $0.12 for the
  server.
- **Controls:** 342 runs, 1 API error (re-run once, as pre-registered), 0
  proxy refusals and 0 pauses. 2 CLI runs were invalid on the leak scan: the
  arms' PATH held the worktree's `.venv/bin` (from `uv run`), and Haiku's
  `which job` printed it. That path is not the key, but the rule voids the
  run. On 10-18 the leak scan found no hits.

### Sonnet 5.5

Results are in [`tiers_2026-10-20/claude-sonnet-5-5/`](tiers_2026-10-20/claude-sonnet-5-5/).
The first 52 runs (rep 1, through h17/h18) ran on the subscription on 10-20;
the other 290 ran on 10-23 on an API key under a spend gate (see the PREREG's
deviations). Same model, prompts and harness; Claude Code 2.1.305/306 then
2.1.310, recorded per row.

| Setup (Sonnet 5.5) | Right / 114 | Questions right in all 3 runs | Cost / answer, median | Seconds / answer, median |
|---|---|---|---|---|
| No tools | 0 | 0 of 38 | $0.0033 | 4.3 |
| + job CLI | 103 | 30 of 38 | $0.059 | 39.9 |
| + jobd-mcp | 111 | 36 of 38 | $0.012 | 6.9 |

- **Primary (Holm p 0.11, not separated):** 8 questions only the server got
  right in all runs, 2 only the CLI. Raw McNemar p = 0.11, CI of the
  difference in share right 0.00 to +0.32. 1 CLI run was voided by the leak
  scan for the same `.venv/bin` PATH line as on Haiku (h31 rep 3, an answer
  that was right); counted as right it is 36 against 31, p = 0.18.
- **Cost and time (secondary):** the server's median answer cost 0.20 of the
  CLI's (CI 0.16 to 0.25) and took 0.17 of the time (0.13 to 0.20); it was
  faster on all 38 questions and cheaper on 37. The arms cost $1.61 (server),
  $7.52 (CLI) and $4.87 (no tools).
- **Mechanism:** every server run called the server, 1.4 times on average.
- **Substitute (secondary, non-concurrent):** Sonnet with the server got 36 of
  38 against Opus with the CLI's 37 of 38 on 10-18 (p = 1).
- **Controls:** 342 runs; 0 invalid server runs, 1 invalid CLI run (leak scan,
  above), 4 invalid no-tools runs. Those 4 (h17, h18, h22 and h37, rep 2, all
  on the API key) hit the 900 s timeout after one short thinking block, and
  timeouts are not re-run. Replayed with the same flags, the same question
  answered in 4 to 8 s every time, so we read them as API-side latency; the
  no-tools arm is not part of the primary.

## Controls

`analyze.py` applies these after a sweep, with no model and no broker. Tests:
`tests/test_agent_qa_controls.py`.

- Oracle: before a sweep, `run.py` stops unless every key answer, formatted
  as a reply, scores right, and a near miss for every question scores wrong
  (exit code + 1, another host, run time + 3 s, one extra job id, the log line
  with text appended, one extra worker). On the real key, 41 of 41 scored
  right and 41 of 41 near misses scored wrong. CI runs the same check on a
  synthetic key (100% and 0%) and checks that it stops a scorer that is too
  lenient or too strict.
- Baselines: two rows with no model, scored by the same `score.py`. The
  constant guess is fixed without the key: exit code 0, the busiest worker
  (3,305 of 6,001 finished jobs), the median run time (144 s), no failures,
  "Done." as the last log line, and every online worker. It gets 4 of 41
  (3 exit codes, 1 host). The best constant takes, per kind, the key's most
  common answer. It is a ceiling on any constant rather than a usable
  strategy, and gets 10 of 41 (3 exit codes, 2 hosts, 2 worker sets, and 1
  each of run time, failures and log line).
- Validity: one rule for every setup. A run counts only if it exited 0 with a
  result, the init event listed exactly that setup's tools (none, `Bash`, or
  the jobd server's six read tools, never a mutating one), a setup with tools
  made at least one call (no model can know a job's exit code), and the leak
  scan found nothing. Nothing is retried; a timeout or harness error would
  stay in as an invalid run. All 369 runs were valid, 0 invalid in each setup
  ([`validity.tsv`](validity.tsv)), and the read-only proxy refused no
  request.
- Leak scan: every tool input, tool output and proxied request path in all
  369 transcripts is searched for the key and result file names, this
  benchmark's directory (locally or on GitHub, including
  raw.githubusercontent.com), this repo's worktrees, and the pilot and run
  directories. Hits: 0. Other repos' worktree paths are not counted, because
  jobs ran there and their records name them.

## Limitations

- The key comes from the broker the tools read, so we measure whether a
  lookup is carried out correctly. We do not measure whether the broker is
  right.
- Both tool setups got everything right, so the questions cannot rank them on
  accuracy. Harder questions (several lookups, judgment) might.
- The questions come from one broker: four workers and about 6,700 jobs.
  Another fleet's history may be easier or harder to search.
- The questions are read-only. Nothing is submitted, cancelled or preempted,
  so the benchmark says nothing about doing those well.
- One model, one date.

## Method

### Setups

Each run is a separate `claude -p` with Claude Opus 5.5:

- Claude alone: no tools.
- Claude + job CLI: Bash, allowed to run only `job` and a few read-only text
  tools (`grep`, `jq`, `date`, …) to filter its output. This is what a user
  has without the server.
- Claude + jobd-mcp: the server only, without `jobd_cancel`, `jobd_preempt`
  and `jobd_worker_delete`.

Every run gets `--setting-sources ""`, an empty working directory,
`--strict-mcp-config` (no other MCP servers) and the same short system
prompt. The user prompt names the client `job` in every setup.

### Questions

`build_questions.py` draws 41 questions at random (seed 20261016) from the
broker's job history and fleet and freezes the answers in `questions.json`.
That file names the broker owner's projects, jobs and log lines, so it is kept
outside the repo with the results and transcripts
(`~/jobd-artifacts/agent_qa/`, or `$JOBD_QA_ARTIFACTS`).

| Kind | Count | Answer key |
|---|---|---|
| Exit code of a job (3 completed, 4 failed) | 7 | `GET /jobs/{id}` |
| Host that ran a job | 7 | `GET /jobs/{id}`, plus the host's own aliases from `GET /workers` |
| Run time of a job (one per seventh of the duration distribution) | 7 | `finished_at - started_at` |
| Every failed job of one project on one UTC day | 6 | `GET /jobs?state_filter=failed`, filtered by the builder |
| Last non-empty line of a job's log | 7 | `GET /jobs/{id}/output` |
| Which workers a job with given tags could run on, the largest GPU tier, the most job slots | 7 | `GET /workers`, cross-checked against a dry-run `POST /submit` |

The builder reads the broker's HTTP API over httpx and never imports `jobd`,
so a bug in jobd-mcp or its client cannot also be in the key. It uses only
jobs that reached a terminal state more than an hour before the freeze, so
their fields cannot change. Logs and worker configuration can still go stale,
so `run.py` re-reads the broker before a sweep and stops if either has. Logs
are pruned after about 60 days, so log questions use jobs from the last 21
days. Projects whose names fold together (`Foo`/`foo`, `a-b`/`a_b`) are left
out, because the broker's project filter matches the folded name.

### Scoring

Each reply ends with `ANSWER: <value>` or `ANSWER: UNKNOWN`, and `score.py`
reads only that line: right, wrong, declined (`UNKNOWN`) or no answer. `NONE`
("no job failed") is a claim, so it is scored as a value. Exit codes must be
equal, sign included (jobd records a SIGTERM as -15). Hosts match any name the
host goes by, case ignored. Run times are right within 1 second; every surface
shows timestamps to the microsecond, so rounding moves an honest answer by
less than that. Failed-job lists and worker sets must be the exact set, with
aliases read as their host. Log lines must be equal after trimming,
collapsing whitespace and removing quotes around the whole line, with case
counted. A wrong host or worker answer naming no host the broker has ever known is
also flagged `unknown_host`, checked against its host list. Tests:
`tests/test_agent_qa_score.py`.

### Safety

The broker runs real work, so no setup can change anything on it. Every setup
reaches it only through `readonly_proxy.py`, a loopback proxy started for each
run. It holds the token and passes only GETs (except `/gpu-holders`),
`POST /submit` with `"dry_run": true`, `POST /resolve` and `POST /classify`;
anything else gets a 403. The token is removed from the setup's environment,
so a request that skips the proxy gets a 401. The CLI setup's `job` is
`job_guard.py`, run from a copy in the run's temp directory: it allows only
read-only subcommands, runs `submit` only as `submit --explain`, and points
the real CLI at the proxy without the token. `--disallowedTools` also names
the mutating MCP tools and `job` subcommands, and `run.py` stops the sweep if
a `job` command that talks to the broker leaves no trace in the proxy log.
`tests/test_agent_qa_guards.py` runs the real CLI through the guard and proxy
against a fake broker and checks, in the same test, that mutating commands
are refused and read-only ones go through.

### Versions

`job` from `jobd==0.5.51` and `jobd-mcp` from `jobd[mcp]==0.5.51`, both from
PyPI through `uvx`. The broker ran 0.5.51 on all four workers. Claude Code
2.1.302, model `claude-opus-5-5`, recorded per run. Before a sweep, `run.py`
stops unless the broker's `/health` version is the one the key was built on
(answers depend on its routing and terminal-state rules) and both pinned
packages report 0.5.51.

The sweep ran on 2026-10-16 (US Eastern) and cost $10.52 in API usage.
Transcripts and proxy logs are kept in `runs/` in the artifacts directory.

## History

1. Pilot 1 (q04 and q35, 1 run each): alone declined both, the server got
   both right, and the CLI setup looked for `jobd` (the server binary), was
   denied, and declined both. Fix: every prompt names `job`, and the `jobd`
   binaries are denied by name.
2. Pilot 2: the CLI setup got both right, but the proxy logged nothing,
   because the Bash tool's zsh re-read `~/.zshenv` and picked up the live
   broker's address and token. Every command was read-only and still
   filtered by the guard. Fix: the guard forces the proxy address and drops
   the token, the setup gets an empty `ZDOTDIR`, and `run.py` stops if the
   proxy sees nothing. Pilot 1 and 2 transcripts were deleted; their verdicts
   are given here.
3. Pilot 3: all 6 runs as expected, every CLI request in the proxy log. Rows
   kept locally in `~/jobd-artifacts/agent_qa/pilot/`, since they quote
   broker data.
4. Before the sweep: `NONE` became a value instead of a decline, and the guard
   stopped accepting `--explain` placed after `--`, where the CLI passes it to
   the job.
5. The sweep: 369 runs, once, no reruns.
6. Controls (PR #162): baselines, oracle, validity rule, leak scan, statistics
   and version checks; artifacts moved out of the checkout. Rescoring all 369
   replies changed no verdict. The first leak scan also counted other repos'
   worktree paths and flagged 36 runs, all because jobs had run there.

## Reproduce

```bash
export JOBD_URL=... JOBD_API_TOKEN=...                          # the broker to ask about
uv run python benchmarks/agent_qa/build_questions.py             # freeze the key (~/jobd-artifacts/agent_qa/questions.json)
uv run python benchmarks/agent_qa/run.py --reps 3 --workers 1    # oracle + version checks, then 369 claude -p runs; resumable
uv run python benchmarks/agent_qa/run.py --rescore               # re-score stored replies after a key fix
uv run python benchmarks/agent_qa/analyze.py                     # validity, leak scan, baselines, stats.tsv
Rscript benchmarks/agent_qa/plot.R                               # summary.tsv + figures
```

The 2026-10-18 re-run (the PREREG's analysis block has the analysis):

```bash
R=~/jobd-artifacts/agent_qa/rerun-2026-10-18
uv run python benchmarks/agent_qa/build_hard.py                  # freeze the v2 key in $R
uv run python benchmarks/agent_qa/run.py --key $R/questions.json --runs $R/runs \
  --results $R/results.jsonl --arms alt,mcp --reps 3 --workers 1 \
  --server-spec git+https://github.com/musharna/jobd@5475cab81055001c10e9a60cd52ebeb06378a3b4 \
  --exclude-newer 2026-10-18T12:00:00Z
Rscript benchmarks/agent_qa/plot.R $R/scored.jsonl benchmarks/agent_qa/rerun_2026-10-18
```
