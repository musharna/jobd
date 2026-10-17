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

Results: TODO, the full sweep has not run yet. Only a pilot has (2 questions ×
3 setups × 1 run, on 2026-10-16 with Claude Code 2.1.302): every setup exited
cleanly and every reply had a scoreable `ANSWER:` line.

Figure: TODO (`agent_qa.png`, written by `plot.R` after the sweep).

| Setup | Right | Wrong | Declined | Questions right in all 3 runs | Cost / answer, median (mean) | Seconds / answer, median (mean) | Slowest |
|---|---|---|---|---|---|---|---|
| Claude alone | TODO | TODO | TODO | TODO of 41 | TODO | TODO | TODO |
| Claude + job CLI | TODO | TODO | TODO | TODO of 41 | TODO | TODO | TODO |
| Claude + jobd-mcp | TODO | TODO | TODO | TODO of 41 | TODO | TODO | TODO |

Out of 123 answers per setup. The three runs of a question are not
independent, so the questions column is the fairer count. The numbers come
from `summary.tsv`, which `plot.R` writes from `results.jsonl`.

## What it shows

TODO after the sweep.

## What it does not show

- **The answer key comes from the broker the tools read.** So the benchmark
  measures whether a lookup is carried out correctly, as the plant-genomics-mcp
  benchmark does. It does not measure whether the broker is right.
- **One broker, one fleet.** The questions are drawn from one person's broker
  (four workers, about 6,700 jobs). Another fleet's history may be easier or
  harder to search.
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
something up. Raw transcripts and proxy logs go to `runs/`, which is
gitignored.
