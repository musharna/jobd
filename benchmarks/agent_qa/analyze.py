"""Controls and statistics for a finished sweep. Runs no model and no broker.

    python benchmarks/agent_qa/analyze.py [--results PATH]

Reads ``questions.json``, ``results.jsonl`` and the transcripts under
``runs/`` in ``ARTIFACTS`` and writes:

* ``ARTIFACTS/scored.jsonl``: every sweep row with ``valid`` and
  ``invalid_reason`` added, followed by the no-model baseline rows. ``plot.R``
  draws from this file. It holds replies and key answers, so it stays out of
  the repo like ``results.jsonl``.
* ``benchmarks/agent_qa/stats.tsv``: bootstrap intervals and paired tests.
* ``benchmarks/agent_qa/validity.tsv``: valid and invalid runs per arm, and
  why, including the leak-scan hits.

Validity is one rule for every arm. A run counts only if it exited 0 with a
result; the tools its arm is meant to have were the ones the init event
listed (none for bare, ``Bash`` for alt, the connected jobd server's read
tools for mcp, never a mutating one); a tool arm made at least one tool call
(every question needs broker data no model can know); and no tool input or
output mentions the key, the results, the benchmark's own files or the run
directories (the leak scan). Nothing is retried in any arm, so an invalid run
stays in the counts as invalid.

Baselines go through the same ``score()`` as the model replies:

* ``prior``: one constant per kind of question, picked without the key from
  what anyone who knows the fleet would guess: exit code 0, the busiest worker
  (laptop ran 3,305 of 6,001 finished jobs), the median finished job's run
  time (144 s), no failures, "Done." as the last log line, and every worker
  the key's fleet snapshot lists as online.
* ``best_constant``: per kind of question, the single answer that is right
  most often, chosen by looking at the key. It is a ceiling on any constant
  guess, not a strategy anyone could follow.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
from collections import Counter, defaultdict
from math import comb
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_questions import ARTIFACTS
from score import oracle_reply, score

HERE = Path(__file__).resolve().parent
ARMS = ["bare", "alt", "mcp"]
SEED = 20261016
BOOT = 10_000
READ_MCP = {
    f"mcp__jobd__jobd_{t}" for t in ("events", "list", "logs", "status", "submit", "workers")
}
MUTATING_MCP = {f"mcp__jobd__jobd_{t}" for t in ("cancel", "preempt", "worker_delete")}

# What a run must never have seen: the key and results by name, the benchmark
# code in this repo or on GitHub, and every place runs and pilots were kept
# (inside this repo's worktree during the sweep, the artifacts dir since).
# Other repos' worktree paths are not a leak: jobs on the broker ran there, so
# they appear in job records the questions are about.
LEAK = re.compile(
    r"questions\.json|results\.jsonl|scored\.jsonl|summary\.tsv|pilot\d?_results"
    r"|benchmarks/agent_qa|agent_qa/|musharna/jobd|github\.com/[^\s\"']*jobd"
    r"|raw\.githubusercontent|jobd/\.claude/worktrees|jobd-bench-scratch|jobd-artifacts"
    r"|/runs/(?:bare|alt|mcp)/",
    re.IGNORECASE,
)

# Fixed before the key was looked at. A last log line has no sensible guess,
# so its constant can only be right by luck; it is in the row so the row
# covers every kind of question.
PRIOR = {
    "exit_code": "0",
    "host": "laptop",
    "wall_time": "144",
    "project_failures": "none",
    "log_line": "Done.",
}


def tool_text(events: list[dict[str, Any]]) -> list[str]:
    """Every tool input and every tool output in a transcript, as text."""
    out = []
    for e in events:
        msg = e.get("message")
        for b in (msg.get("content") if isinstance(msg, dict) else None) or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                out.append(json.dumps(b.get("input")))
            elif b.get("type") == "tool_result":
                c = b.get("content")
                out.append(c if isinstance(c, str) else json.dumps(c))
    return out


def leak_hits(transcript: Path) -> list[str]:
    events = [json.loads(ln) for ln in transcript.read_text().splitlines() if ln.startswith("{")]
    texts = tool_text(events)
    proxy = transcript.with_suffix(".proxy.jsonl")
    if proxy.exists():
        texts += [json.loads(ln).get("path", "") for ln in proxy.read_text().splitlines()]
    return sorted({m.group(0) for t in texts for m in LEAK.finditer(t)})


def invalid_reason(r: dict[str, Any], hits: list[str], has_transcript: bool) -> str | None:
    if r["exit"] != 0:
        return f"exit {r['exit']}"
    if not has_transcript or r.get("cost_usd") is None:
        return "no result event"
    tools = set(r["tools_available"])
    servers = {s["name"]: s["status"] for s in r.get("mcp_servers") or []}
    if tools & MUTATING_MCP:
        return "mutating tool offered"
    want = {"bare": set(), "alt": {"Bash"}, "mcp": READ_MCP}[r["arm"]]
    if tools != want or (r["arm"] == "mcp" and servers.get("jobd") != "connected"):
        return "wrong tools"
    if r["arm"] != "bare" and not r["tool_calls"]:
        return "no tool call"
    if hits:
        return "leak: " + ", ".join(hits)
    return None


def baseline_rows(questions: list[dict[str, Any]], fleet: list[dict[str, Any]]) -> list[dict]:
    online = ", ".join(w["host"] for w in fleet if w["state"] == "online")
    prior = {**PRIOR, "fleet": online}
    by_cat = defaultdict(list)
    for q in questions:
        by_cat[q["category"]].append(q)
    best: dict[str, str] = {}
    for cat, qs in by_cat.items():
        # Candidates: every answer the key holds for this kind of question.
        cands = sorted({oracle_reply(q) for q in qs})
        best[cat] = max(
            cands, key=lambda c: (sum(score(q, c)["verdict"] == "right" for q in qs), c)
        )
    rows = []
    for name, reply_for in (
        ("prior", lambda q: f"ANSWER: {prior[q['category']]}"),
        ("best_constant", lambda q: best[q["category"]]),
    ):
        for q in questions:
            rows.append(
                {
                    "arm": name, "qid": q["id"], "category": q["category"], "rep": 1,
                    "baseline": True, "valid": True, "invalid_reason": None,
                    "cost_usd": None, "seconds": None, "tool_calls": [],
                    **score(q, reply_for(q)),
                }
            )  # fmt: skip
    return rows


def binom_two_sided(k: int, n: int) -> float:
    """Exact two-sided sign-test p for k of n successes at p = 1/2."""
    if n == 0:
        return 1.0
    lo = min(k, n - k)
    return min(1.0, 2 * sum(comb(n, i) for i in range(lo + 1)) / 2**n)


def per_question(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out = defaultdict(list)
    for r in rows:
        out[r["qid"]].append(r)
    return out


def metrics(qs: list[list[dict[str, Any]]]) -> dict[str, float]:
    runs = [r for q in qs for r in q]
    return {
        "accuracy_runs": sum(r["verdict"] == "right" for r in runs) / len(runs),
        "questions_right_every_run": sum(all(r["verdict"] == "right" for r in q) for q in qs)
        / len(qs),
        "cost_usd_median": statistics.median(r["cost_usd"] for r in runs),
        "cost_usd_mean": statistics.fmean(r["cost_usd"] for r in runs),
        "seconds_median": statistics.median(r["seconds"] for r in runs),
        "seconds_mean": statistics.fmean(r["seconds"] for r in runs),
        "tool_calls_mean": statistics.fmean(len(r["tool_calls"]) for r in runs),
    }


def bootstrap(by_arm: dict[str, dict[str, list[dict]]], qids: list[str]) -> list[dict]:
    """Percentile 95% intervals, resampling questions (a question's runs move
    together, since repeats of one question are not independent)."""
    rng = random.Random(SEED)
    draws: dict[tuple[str, str], list[float]] = defaultdict(list)
    for _ in range(BOOT):
        sample = [rng.choice(qids) for _ in qids]
        got = {a: metrics([by_arm[a][q] for q in sample]) for a in by_arm}
        for a, m in got.items():
            for k, v in m.items():
                draws[(a, k)].append(v)
        if "alt" in got and "mcp" in got:
            draws[("mcp/alt", "seconds_median_ratio")].append(
                got["mcp"]["seconds_median"] / got["alt"]["seconds_median"]
            )
            draws[("mcp/alt", "cost_usd_median_ratio")].append(
                got["mcp"]["cost_usd_median"] / got["alt"]["cost_usd_median"]
            )
            draws[("mcp/alt", "cost_usd_mean_ratio")].append(
                got["mcp"]["cost_usd_mean"] / got["alt"]["cost_usd_mean"]
            )
            draws[("mcp-alt", "tool_calls_mean_diff")].append(
                got["mcp"]["tool_calls_mean"] - got["alt"]["tool_calls_mean"]
            )
    full = {a: metrics([by_arm[a][q] for q in qids]) for a in by_arm}
    point = {(a, k): v for a, m in full.items() for k, v in m.items()}
    if "alt" in full and "mcp" in full:
        point[("mcp/alt", "seconds_median_ratio")] = (
            full["mcp"]["seconds_median"] / full["alt"]["seconds_median"]
        )
        point[("mcp/alt", "cost_usd_median_ratio")] = (
            full["mcp"]["cost_usd_median"] / full["alt"]["cost_usd_median"]
        )
        point[("mcp/alt", "cost_usd_mean_ratio")] = (
            full["mcp"]["cost_usd_mean"] / full["alt"]["cost_usd_mean"]
        )
        point[("mcp-alt", "tool_calls_mean_diff")] = (
            full["mcp"]["tool_calls_mean"] - full["alt"]["tool_calls_mean"]
        )
    out = []
    for key, vals in draws.items():
        vals.sort()
        out.append(
            {
                "arm": key[0], "metric": key[1], "estimate": point[key],
                "lo95": vals[int(0.025 * BOOT)], "hi95": vals[int(0.975 * BOOT) - 1],
                "p": None, "note": f"bootstrap over {len(qids)} questions, {BOOT} draws",
            }
        )  # fmt: skip
    return out


def paired_tests(by_arm: dict[str, dict[str, list[dict]]], qids: list[str]) -> list[dict]:
    alt, mcp = by_arm["alt"], by_arm["mcp"]
    ok = {a: {q: all(r["verdict"] == "right" for r in by_arm[a][q]) for q in qids} for a in by_arm}
    b = sum(ok["mcp"][q] and not ok["alt"][q] for q in qids)
    c = sum(ok["alt"][q] and not ok["mcp"][q] for q in qids)
    out = [
        {
            "arm": "mcp_vs_alt", "metric": "mcnemar_right_every_run", "estimate": b - c,
            "lo95": None, "hi95": None, "p": binom_two_sided(b, b + c),
            "note": f"exact; discordant questions: {b} only mcp right, {c} only alt right",
        }
    ]  # fmt: skip
    for metric, f in (
        ("seconds_median", lambda rs: statistics.median(r["seconds"] for r in rs)),
        ("cost_usd_median", lambda rs: statistics.median(r["cost_usd"] for r in rs)),
        ("tool_calls_median", lambda rs: statistics.median(len(r["tool_calls"]) for r in rs)),
    ):
        lower = sum(f(mcp[q]) < f(alt[q]) for q in qids)
        higher = sum(f(mcp[q]) > f(alt[q]) for q in qids)
        out.append(
            {
                "arm": "mcp_vs_alt", "metric": f"sign_test_{metric}", "estimate": lower - higher,
                "lo95": None, "hi95": None, "p": binom_two_sided(lower, lower + higher),
                "note": f"per-question medians: mcp lower on {lower}, higher on {higher}, "
                f"tied on {len(qids) - lower - higher}",
            }
        )  # fmt: skip
    return out


def write_tsv(path: Path, rows: list[dict[str, Any]]) -> None:
    cols = list(rows[0])

    def fmt(v: Any) -> str:
        if v is None:
            return "NA"
        if isinstance(v, float):
            return f"{v:.6g}"
        return str(v)

    path.write_text(
        "\t".join(cols) + "\n" + "".join("\t".join(fmt(r[c]) for c in cols) + "\n" for r in rows)
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=str(ARTIFACTS / "results.jsonl"))
    ap.add_argument("--outdir", default=str(HERE))
    a = ap.parse_args()
    key = json.loads((ARTIFACTS / "questions.json").read_text())
    questions = key["questions"]
    by_id = {q["id"]: q for q in questions}
    rows = [json.loads(ln) for ln in Path(a.results).read_text().splitlines()]

    leaks_scanned = 0
    for r in rows:
        # Rescore on the way in, so the analysis never uses a stale verdict.
        r.update(score(by_id[r["qid"]], r["reply"]))
        t = ARTIFACTS / "runs" / r["arm"] / f"{r['qid']}-{r['rep']}.jsonl"
        hits = leak_hits(t) if t.exists() else []
        leaks_scanned += t.exists()
        r["leak_hits"] = hits
        r["invalid_reason"] = invalid_reason(r, hits, t.exists())
        r["valid"] = r["invalid_reason"] is None

    validity = []
    for arm in ARMS:
        rs = [r for r in rows if r["arm"] == arm]
        reasons = Counter(r["invalid_reason"] for r in rs if not r["valid"])
        validity.append(
            {
                "arm": arm, "runs": len(rs), "valid": sum(r["valid"] for r in rs),
                "invalid": len(rs) - sum(r["valid"] for r in rs),
                "transcripts_scanned": sum(
                    (ARTIFACTS / "runs" / arm / f"{r['qid']}-{r['rep']}.jsonl").exists()
                    for r in rs
                ),
                "leak_hits": sum(bool(r["leak_hits"]) for r in rs),
                "reasons": "; ".join(f"{k}: {v}" for k, v in sorted(reasons.items())) or "none",
            }
        )  # fmt: skip
    base = baseline_rows(questions, key["fleet"])
    for name in ("prior", "best_constant"):
        rs = [r for r in base if r["arm"] == name]
        validity.append(
            {
                "arm": name, "runs": len(rs), "valid": len(rs), "invalid": 0,
                "transcripts_scanned": 0, "leak_hits": 0, "reasons": "no model: scorer only",
            }
        )  # fmt: skip

    valid = [r for r in rows if r["valid"]]
    qids = [q["id"] for q in questions]
    by_arm = {a: per_question([r for r in valid if r["arm"] == a]) for a in ("alt", "mcp")}
    complete = [q for q in qids if all(by_arm[a].get(q) for a in by_arm)]
    stats = bootstrap(by_arm, complete) + paired_tests(by_arm, complete)
    for name in ("bare", "prior", "best_constant"):
        rs = [r for r in (valid if name == "bare" else base) if r["arm"] == name]
        right_q = sum(all(r["verdict"] == "right" for r in g) for g in per_question(rs).values())
        stats.append(
            {
                "arm": name, "metric": "questions_right_every_run",
                "estimate": right_q / len(qids), "lo95": None, "hi95": None, "p": None,
                "note": f"{right_q} of {len(qids)} questions",
            }
        )  # fmt: skip

    (ARTIFACTS / "scored.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [*rows, *base]))
    out = Path(a.outdir)
    write_tsv(out / "validity.tsv", validity)
    write_tsv(out / "stats.tsv", stats)
    print(f"{leaks_scanned} transcripts scanned for leaks")
    for v in validity:
        print(v)
    for s in stats:
        print({k: (round(v, 4) if isinstance(v, float) else v) for k, v in s.items()})


if __name__ == "__main__":
    main()
