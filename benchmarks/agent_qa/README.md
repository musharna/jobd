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

Run on 2026-10-16 with Claude Code 2.1.302. All 369 runs exited 0, and the
read-only proxy refused no request in any of them.

![Right, wrong or declined per setup](agent_qa.png)

| Setup | Right | Wrong | Declined | Questions right in all 3 runs | Cost / answer, median (mean) | Seconds / answer, median (mean) | Slowest | Tool calls / answer, median (mean) |
|---|---|---|---|---|---|---|---|---|
| Claude alone | 0 | 0 | 123 | 0 of 41 | $0.009 ($0.010) | 4.8 (5.4) | 18 s | 0 (0) |
| Claude + job CLI | 123 | 0 | 0 | 41 of 41 | $0.028 ($0.038) | 13.6 (16.4) | 55 s | 2 (2.9) |
| Claude + jobd-mcp | 123 | 0 | 0 | 41 of 41 | $0.022 ($0.038) | 6.7 (8.2) | 26 s | 1 (1.8) |

Out of 123 answers per setup. The three runs of a question are not
independent, so the questions column is the fairer count. The numbers come
from [`summary.tsv`](summary.tsv), which `plot.R` writes from `results.jsonl`.

## What it shows

- **Accuracy is a tie.** With the CLI and with the server, Claude got every
  question right in every run. Alone, it declined all 123. It did not guess
  once.
- **The server's edge is time and calls, not cost.**
  - The typical answer took half as long with the server: a median of 6.7 s
    against 13.6 s, with means of 8.2 s against 16.4 s. The server was faster
    in every kind of question.
  - It made fewer tool calls: a median of 1 against 2. With the CLI, every one of the 123 runs
    started by reading `job --help`.
  - Mean cost was the same: $0.038 for both, $4.66 for the server's 123
    answers against $4.65 for the CLI's. The median favoured the server
    ($0.022 against $0.028).
- **One question set the server's mean cost.** It asked for one project's
  failed jobs on one day (q26), and cost $0.33 to $0.46 per run with the
  server, against $0.13 to $0.17 with the CLI. `jobd_list` rows carry no
  `finished_at` and the tool has no date filter, so Claude fetched the
  project's 200 most recent failures (47,000 characters) and then whole event
  streams (up to 54,000 characters) to find the day. With the CLI it read
  `job status` for each job and grepped the `finished` line.
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

## Reproduce

```bash
export JOBD_URL=... JOBD_API_TOKEN=...                          # the broker to ask about
uv run python benchmarks/agent_qa/build_questions.py             # freeze the key (questions.json)
uv run python benchmarks/agent_qa/run.py --reps 3 --workers 1    # 369 claude -p runs; resumable
uv run python benchmarks/agent_qa/run.py --rescore               # re-score stored replies after a key fix
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
`runs/`, which is gitignored.
