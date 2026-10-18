# Agent benchmark: questions about a live broker, with and without jobd-mcp

Does Claude answer questions about a jobd broker better with jobd-mcp than
with the `job` CLI? 41 questions, asked 3 times each to Claude Opus 5.5 in
Claude Code under three setups:

- **Claude alone**: no tools.
- **Claude + job CLI**: Bash, allowed to run only `job` (jobd 0.5.51 from PyPI)
  and a few read-only text tools (`grep`, `jq`, `date`, …) to filter its
  output. This is what a user has without the server.
- **Claude + jobd-mcp**: this server (jobd[mcp] 0.5.51 from PyPI) only,
  without `jobd_cancel`, `jobd_preempt` and `jobd_worker_delete`.

Run on 2026-10-16 with Claude Code 2.1.302. All 369 runs exited 0 and are
valid under the rule in [Controls](#controls), and the read-only proxy refused
no request in any of them. Two baselines with no model at all are scored by
the same scorer, to show what guessing earns.

![Right, wrong or declined per setup](agent_qa.png)

| Setup | Right | Wrong | Declined | Questions right in all 3 runs | Cost / answer, median (mean) | Seconds / answer, median (mean) | Slowest | Tool calls / answer, median (mean) |
|---|---|---|---|---|---|---|---|---|
| Constant guess, no model (1 answer per question) | 4 | 37 | 0 | 4 of 41 | – | – | – | – |
| Best constant, fit to the key, no model (1 per question) | 10 | 31 | 0 | 10 of 41 | – | – | – | – |
| Claude alone | 0 | 0 | 123 | 0 of 41 | $0.009 ($0.010) | 4.8 (5.4) | 18 s | 0 (0) |
| Claude + job CLI | 123 | 0 | 0 | 41 of 41 | $0.028 ($0.038) | 13.6 (16.4) | 55 s | 2 (2.9) |
| Claude + jobd-mcp | 123 | 0 | 0 | 41 of 41 | $0.022 ($0.038) | 6.7 (8.2) | 26 s | 1 (1.8) |

Out of 123 answers per Claude setup and 41 per baseline. The three runs of a
question are not independent, so the questions column is the fairer count.
The numbers come from [`summary.tsv`](summary.tsv); the intervals and tests
below come from [`stats.tsv`](stats.tsv). `analyze.py` and `plot.R` write both
from the stored runs.

## What it shows

All intervals are 95% bootstrap intervals over the 41 questions (10,000
draws, seed 20261016). A question's three runs are resampled together.

- **The CLI and the server are not separated on accuracy.** Both got all 41
  questions right in all 3 runs. No question was right with one and not the
  other, so the paired exact McNemar test has nothing to work on (0 discordant
  questions, p = 1). At 41 of 41 the questions are too easy to rank the two.
- **They are not separated on mean cost either.** The mean was $0.038 for
  both. The ratio of server to CLI was 1.00 (0.78 to 1.31). Total cost was
  $4.66 for the server's 123 answers and $4.65 for the CLI's.
- **What does separate them is time, calls and median cost:**
  - **Time.** The server's median answer took 6.7 s (6.1 to 7.7) against
    13.6 s (11.2 to 15.8). That is a ratio of 0.49 (0.44 to 0.59). The
    server's median was lower on all 41 questions (sign test p < 1e-11). The
    means were 8.2 s against 16.4 s.
  - **Calls.** The server made 1.8 tool calls per answer on average, against
    2.9 with the CLI. The difference is −1.1 (−1.5 to −0.8), and the server
    used fewer on 37 questions, more on 3, with 1 tie. With the CLI, every one
    of the 123 runs started by reading `job --help`.
  - **Median cost.** $0.022 against $0.028, a ratio of 0.76 (0.67 to 0.81).
    The server was cheaper on 35 questions and dearer on 6.
- **Mean cost is equal while median cost is not because of one question.**
  It asked for one project's failed jobs on one day (q26), and cost $0.33 to
  $0.46 per run with the server, against $0.13 to $0.17 with the CLI.
  `jobd_list` rows carry no `finished_at` and the tool has no date filter, so
  Claude fetched the project's 200 most recent failures (47,000 characters)
  and then whole event streams (up to 54,000 characters) to find the day. With
  the CLI it read `job status` for each job and grepped the `finished` line.
- **Without tools, Claude declined all 123 and did not guess once.** A
  constant guess gets 4 of 41 questions right. The best constant per kind of
  question, chosen by looking at the key, gets 10 of 41. Both tool setups are
  far above either.
- **The server's 5 `jobd_submit` calls were all dry runs** (routing
  questions). The CLI setup has no dry run, and it answered routing questions
  from `job workers` tags.

![Cost and time per answer, every run](agent_qa_cost_time.png)

![Right, wrong or declined by kind of question](agent_qa_by_category.png)

## What it does not show

- **The answer key comes from the broker the tools read.** So the benchmark
  measures whether a lookup is carried out correctly, as the plant-genomics-mcp
  benchmark does. It does not measure whether the broker is right.
- **One broker, one fleet.** The questions are drawn from one person's broker
  (four workers, about 6,700 jobs). Another fleet's history may be easier or
  harder to search.
- **Every setup with tools got everything right, so the questions cannot rank
  the two on accuracy.** Harder questions (several lookups, judgement) might.
  This is a ceiling effect, not evidence that the two are equally accurate.
- **Read-only questions only.** Nothing is submitted, cancelled or preempted,
  so the benchmark says nothing about doing those well.
- **One model, one date.**

## Questions

`build_questions.py` draws 41 questions at random (seed 20261016) from the
broker's job history and fleet, and freezes the answers with a timestamp in
`questions.json`. That file names the broker owner's projects, jobs and log
lines, so it is gitignored. The questions:

| Kind | Count | Answer key |
|---|---|---|
| Exit code of a job (3 completed, 4 failed) | 7 | `GET /jobs/{id}` |
| Host that ran a job | 7 | `GET /jobs/{id}`, plus the host's own aliases from `GET /workers` |
| Run time of a job (one per seventh of the duration distribution) | 7 | `finished_at - started_at` |
| Every failed job of one project on one UTC day | 6 | `GET /jobs?state_filter=failed`, filtered by the builder |
| Last non-empty line of a job's log | 7 | `GET /jobs/{id}/output` |
| Which workers a job with given tags could run on, the largest GPU tier, the most job slots | 7 | `GET /workers`, cross-checked against a dry-run `POST /submit` |

The builder reads the broker's HTTP API over httpx and never imports `jobd`,
so a bug in jobd-mcp or in jobd's client cannot also be in the key. It uses
only jobs in a terminal state that finished more than an hour before the
freeze, so their fields cannot change.

Two kinds of question can still go stale, and `run.py` re-reads the broker
before a sweep and stops if either has:

- **Logs** are pruned after about 60 days, so log questions use jobs from the
  last 21 days.
- **Worker configuration** (tags, slots, which hosts are online) changes when
  someone reconfigures a host.

Projects whose names fold together (`Foo`/`foo`, `a-b`/`a_b`) are left out of
the failures questions, because the broker's project filter matches the folded
name.

## Scoring

Each reply ends with `ANSWER: <value>` or `ANSWER: UNKNOWN`. `score.py`
decides the verdict from that line alone:

- **right**: the value matches the key.
- **wrong**: the value does not match.
- **declined**: `UNKNOWN`. Here `NONE` is a claim ("no job failed"), not a
  refusal, so it is scored as a value.
- **no answer**: there is no `ANSWER:` line.

Matching rules by kind:

- **Exit codes** must be equal, sign included: jobd records a SIGTERM as -15.
- **Hosts** match any name the host goes by, case ignored.
- **Run times** are right within 1 second. Every surface shows the timestamps
  to the microsecond, and rounding or truncating to whole seconds moves an
  honest answer by less than that.
- **Failed-job lists** must be the exact set, in any order.
- **Log lines** are quotations: equal after trimming, collapsing whitespace and
  removing quotes wrapped around the whole line. Case counts.
- **Worker sets** must be the exact set, with aliases read as their host.

A wrong host or worker answer that names no host the broker has ever known is
also flagged `unknown_host`. The scorer checks the name against the broker's
list of hosts; it does not judge it by shape.
Tests: `tests/test_agent_qa_score.py`.

## Safety

The questions are about a live broker that runs real work, so no setup can
change anything on it:

- **Read-only proxy.** Every setup reaches the broker only through
  `readonly_proxy.py`, a loopback proxy started for each run. The proxy holds
  the token and lets through only:
  - GETs (except `/gpu-holders`),
  - `POST /submit` with `"dry_run": true`,
  - `POST /resolve` and `POST /classify`.

  Everything else gets a 403 and never reaches the broker. The token is
  removed from the setup's environment, so a request that skips the proxy is
  refused with a 401.
- **The `job` the CLI setup runs** is `job_guard.py`. It allows only
  read-only subcommands, and runs `submit` only as `submit --explain`. It
  points the real CLI at the proxy and drops the token itself. The pilot
  showed why: Claude Code's Bash tool re-reads `~/.zshenv`, which put the
  broker's real address and token back.
- **`--disallowedTools`** also names the mutating MCP tools and `job`
  subcommands.
- **`run.py` stops the sweep** if a `job` command that talks to the broker
  leaves no trace in the proxy log.

Tests (`tests/test_agent_qa_guards.py`) run the real CLI through the guard and
the proxy against a fake broker. They check that each mutating command is
refused, and in the same test that read-only ones still go through.

## Controls

`analyze.py` applies these after a sweep; it runs no model and no broker.
Tests: `tests/test_agent_qa_controls.py`.

- **Baselines (negative controls).** Two rows with no model, scored by the
  real `score.py`:
  - *Constant guess.* One answer per kind, fixed without the key: exit code 0,
    the busiest worker (it ran 3,305 of 6,001 finished jobs), the median
    finished job's run time (144 s), no failures, "Done." as the last log
    line, every online worker. It gets 4 of 41 (3 exit codes, 1 host).
  - *Best constant.* Per kind, the one answer from the key that is right most
    often. This is a ceiling on any constant, not a strategy anyone could
    use. It gets 10 of 41: 3 exit codes, 2 hosts, 2 worker sets, and 1 each of
    run time, failures and log line.
- **Oracle (positive control).** Before a sweep, `run.py` formats every key
  answer as a reply and stops unless all of them score right. It also stops
  unless a near miss for every question scores wrong: exit code + 1, another
  host, run time + 3 s, one extra job id, the log line with text appended, or
  one extra worker. On the real key: 41 of 41 right and 41 of 41 wrong. CI
  runs the same check on a synthetic key in the same shape (100% and 0%), and
  checks that the pre-sweep check stops a scorer that is too lenient or too
  strict.
- **Validity: one rule for every setup.** A run counts only if all of these
  hold:
  - it exited 0 with a result;
  - the init event listed exactly that setup's tools: none, `Bash`, or the
    connected jobd server's six read tools, and never a mutating one;
  - a setup with tools made at least one call (no model can know a job's exit
    code);
  - the leak scan found nothing.

  Nothing is retried in any setup. A timeout or harness error stays in the
  results as an invalid run. All 369 runs are valid: 0 invalid in each setup
  ([`validity.tsv`](validity.tsv)).
- **Leak scan.** Every tool input, tool output and proxied request path in all
  369 transcripts is searched for the key and result files by name, this
  benchmark's directory (locally or on GitHub, including
  raw.githubusercontent.com), this repo's worktrees, and the pilot and run
  directories. Hits: 0. Other repos' worktree paths are not counted, because
  jobs ran there and their records name them; a first version of the scan
  that counted them flagged 36 runs, all for that reason.
  Since this round, the key, results and transcripts live outside the
  checkout (`~/jobd-artifacts/agent_qa/`, or `$JOBD_QA_ARTIFACTS`). The `job`
  wrapper runs a copy of `job_guard.py` from the run's temp directory, so
  nothing a run can reach points into the repo.

## Versions

- **CLI setup:** `job` from `jobd==0.5.51` (PyPI), run through `uvx`.
- **Server setup:** `jobd-mcp` from `jobd[mcp]==0.5.51` (PyPI), run through
  `uvx`.
- **Broker:** 0.5.51, on all four workers.
- **Claude Code:** 2.1.302, model `claude-opus-5-5`, recorded per run from
  the init event.

Before a sweep, `run.py` checks three things and stops if any fails:

- the broker's `/health` version equals the version the key was built on,
  since the answers depend on its routing and terminal-state rules;
- the pinned CLI reports 0.5.51;
- the pinned server's installed `jobd` is 0.5.51.

## History

Everything changed between the first run and the results above:

1. **Pilot 1** (questions q04 and q35, 1 run each). Claude alone declined
   both. The server got both right. The CLI setup declined both: the prompt
   did not name the client, so Claude looked for `jobd` (the server binary),
   was denied, and gave up. That measures guessing a name. Fix: every setup's
   prompt names the client `job`, and the `jobd` binaries are denied by name.
2. **Pilot 2** (same questions). The CLI setup got both right, but the proxy
   logged no request: the Bash tool's zsh re-read `~/.zshenv`, which exported
   the live broker's address and token. So `job` went around the proxy. Every
   command it ran was read-only, and the guard was still filtering
   subcommands. Fixes:
   - the guard forces the proxy address and drops the token itself;
   - the setup gets an empty `ZDOTDIR`;
   - `run.py` stops the sweep if `job` talks to the broker and the proxy sees
     nothing.

   The transcripts of pilots 1 and 2 were deleted; their verdicts are given
   here.
3. **Pilot 3** (same questions, after both fixes). All 6 runs went as
   expected: alone declined 2, CLI right 2, server right 2, with every CLI
   request in the proxy log. The rows are archived in
   `~/jobd-artifacts/agent_qa/pilot/` (not in the repo, as they quote broker
   data).
4. **Scorer and guard fixes before the sweep.**
   - `NONE` had been scored as a decline; it is a claim ("no job failed"), so
     it is now scored as a value.
   - The guard had accepted `submit -- … --explain`, with `--explain` after
     `--`, where the CLI passes it to the job. Only options before `--` count
     now.
5. **The sweep** (369 runs) ran once, with no reruns.
6. **This round** added the baselines, oracle check, validity rule, leak
   scan, statistics and version asserts. It also moved the artifacts out of
   the checkout. Rescoring all 369 stored replies with the current scorer
   changed no verdict.

The perf ratchet in `benchmarks/perf/` had its own fixes. Coroutine and
generator resumptions differed across Python versions; on CI's 3.12.3, one
fewer generator resume failed the gate. Each generator or coroutine is now
counted once per entry, and the baseline was rebuilt on 3.12.3. The counts
are identical on 3.11, 3.12.3, 3.12.13, 3.13 and 3.14.

## Reproduce

```bash
export JOBD_URL=... JOBD_API_TOKEN=...                          # the broker to ask about
uv run python benchmarks/agent_qa/build_questions.py             # freeze the key (~/jobd-artifacts/agent_qa/questions.json)
uv run python benchmarks/agent_qa/run.py --reps 3 --workers 1    # oracle + version checks, then 369 claude -p runs; resumable
uv run python benchmarks/agent_qa/run.py --rescore               # re-score stored replies after a key fix
uv run python benchmarks/agent_qa/analyze.py                     # validity, leak scan, baselines, stats.tsv
Rscript benchmarks/agent_qa/plot.R                               # summary.tsv + figures
```

Each run is an isolated `claude -p`:
- `--setting-sources ""` and an empty working directory.
- `--strict-mcp-config`, so no other MCP servers.
- The same short system prompt in every setup.

The user prompt names the CLI (`job`) in every setup too. In the pilot, the
CLI setup looked for a program called `jobd` (the broker's server binary), was
denied and declined, which measured guessing a name rather than looking
something up. The full run cost $10.52 in API usage. Raw transcripts and proxy logs go to
`runs/` in the artifacts directory, outside the repo.
