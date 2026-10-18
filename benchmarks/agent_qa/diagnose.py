"""Where each arm's tokens and dollars go, read from the raw transcripts.

    uv run python benchmarks/agent_qa/diagnose.py <runs_dir> [out.tsv]

``<runs_dir>`` holds ``<arm>/<qid>-<rep>.jsonl`` stream-json transcripts
(``run.py`` writes them). For every run, from the transcript alone:

- the tools the init event lists, and the context the first API call already
  carries (system prompt + tool definitions: its input + cache-write +
  cache-read tokens). The bare arm has no tools, so arm minus bare is what
  the tool definitions cost;
- turns (``num_turns``) and tool calls, by tool;
- characters the model wrote into tool inputs and characters tools returned
  (tool output is paid as context on every later turn);
- tokens by kind, from the result's ``modelUsage``, and the dollars each kind
  cost. The prices are fitted, not assumed: a least-squares fit of
  ``total_cost_usd`` on the four token counts must reproduce every run's cost
  to within 1e-6, or the script stops;
- the mechanism readout: which ``jobd_list`` arguments each run passed, and
  tool calls refused because the model used a name the arm does not have.

Prints per-arm medians, cost shares and mechanism counts; writes one row per
run to ``out.tsv`` when given.
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
from pathlib import Path
from typing import Any

KINDS = ("input", "cache_write", "cache_read", "output")
LIST_ARGS = ("state", "project", "limit", "finished_after", "finished_before")


def _events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.startswith("{")]


def run_row(path: Path) -> dict[str, Any] | None:
    ev = _events(path)
    result = next((e for e in reversed(ev) if e.get("type") == "result"), None)
    if result is None or not result.get("modelUsage"):
        return None
    init = next((e for e in ev if e.get("subtype") == "init"), {})
    tools = init.get("tools") or []
    mu: dict[str, int] = {}
    for m in result["modelUsage"].values():
        for k, src in (
            ("input", "inputTokens"),
            ("cache_write", "cacheCreationInputTokens"),
            ("cache_read", "cacheReadInputTokens"),
            ("output", "outputTokens"),
        ):
            mu[k] = mu.get(k, 0) + int(m.get(src) or 0)
    first = next((e["message"]["usage"] for e in ev if e.get("type") == "assistant"), {})
    base = sum(
        int(first.get(k) or 0)
        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    calls: dict[str, int] = {}
    in_chars = 0
    list_args: set[str] = set()
    seen: set[str] = set()
    names: dict[str, str] = {}
    for e in ev:
        if e.get("type") != "assistant":
            continue
        for b in e["message"].get("content", []):
            if b.get("type") == "tool_use" and b.get("id") not in seen:
                seen.add(b["id"])
                name = b["name"].replace("mcp__jobd__", "")
                names[b["id"]] = b["name"]
                calls[name] = calls.get(name, 0) + 1
                in_chars += len(json.dumps(b.get("input", {})))
                if name == "jobd_list":
                    list_args |= {k for k, v in (b.get("input") or {}).items() if v is not None}
    out_chars: dict[str, int] = {}
    refused_name = 0
    for e in ev:
        if e.get("type") != "user":
            continue
        for b in (e.get("message") or {}).get("content", []):
            if isinstance(b, dict) and b.get("type") == "tool_result":
                text = json.dumps(b.get("content", ""))
                tool = names.get(b.get("tool_use_id"), "?").replace("mcp__jobd__", "")
                out_chars[tool] = out_chars.get(tool, 0) + len(text)
                if b.get("is_error") and "No such tool available" in text:
                    refused_name += 1
    return {
        "arm": path.parent.name,
        "run": path.stem,
        "n_tools": len(tools),
        "base_context": base,
        "turns": result.get("num_turns"),
        "tool_calls": sum(calls.values()),
        "calls": json.dumps(calls, sort_keys=True),
        "tool_input_chars": in_chars,
        "tool_output_chars": sum(out_chars.values()),
        "tool_output_chars_by_tool": json.dumps(out_chars, sort_keys=True),
        "refused_bad_name": refused_name,
        **{f"list_{k}": k in list_args for k in LIST_ARGS},
        **{f"tok_{k}": mu.get(k, 0) for k in KINDS},
        "cost_usd": float(result["total_cost_usd"]),
    }


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (no numpy in jobd's deps)."""
    n = len(b)
    m = [[*row, rhs] for row, rhs in zip(a, b, strict=False)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[piv] = m[piv], m[c]
        if m[c][c] == 0:
            raise SystemExit(f"price fit is singular: no run has any {KINDS[c]} tokens")
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c], strict=False)]
    return [m[i][n] / m[i][i] for i in range(n)]


def fit_prices(rows: list[dict[str, Any]]) -> list[float]:
    """Least squares by the normal equations, on token counts in millions."""
    x = [[r[f"tok_{k}"] / 1e6 for k in KINDS] for r in rows]
    y = [r["cost_usd"] for r in rows]
    ata = [[sum(row[i] * row[j] for row in x) for j in range(4)] for i in range(4)]
    aty = [sum(row[i] * v for row, v in zip(x, y, strict=False)) for i in range(4)]
    per_m = _solve(ata, aty)
    worst = max(
        abs(sum(p * t for p, t in zip(per_m, row, strict=False)) - v)
        for row, v in zip(x, y, strict=False)
    )
    if worst > 1e-6:
        raise SystemExit(f"price fit leaves a residual of ${worst:.3g}; costs are not linear")
    return [p / 1e6 for p in per_m]


def main() -> None:
    runs = Path(sys.argv[1])
    rows = [r for p in sorted(runs.glob("*/*.jsonl")) if (r := run_row(p))]
    rows = [r for r in rows if not r["run"].endswith(".proxy")]
    if not rows:
        raise SystemExit(f"no transcripts under {runs}")
    price = fit_prices(rows)
    print(
        "fitted $ per million tokens:",
        {k: round(p * 1e6, 4) for k, p in zip(KINDS, price, strict=False)},
    )
    for r in rows:
        for k, p in zip(KINDS, price, strict=False):
            r[f"usd_{k}"] = r[f"tok_{k}"] * p
    arms = sorted({r["arm"] for r in rows})
    cols = [
        "n_tools", "base_context", "turns", "tool_calls", "tool_input_chars",
        "tool_output_chars", "tok_output", "tok_cache_write", "tok_cache_read", "cost_usd",
    ]  # fmt: skip
    print("\nmedian per run (mean in brackets for tool output and cost)")
    print("arm    n " + " ".join(f"{c:>17}" for c in cols))
    for arm in arms:
        rs = [r for r in rows if r["arm"] == arm]
        print(f"{arm:5} {len(rs):3} " + " ".join(
            f"{statistics.median(r[c] for r in rs):>17.4g}" for c in cols))  # fmt: skip
        print(f"{'':9} mean tool output {statistics.fmean(r['tool_output_chars'] for r in rs):.0f}"
              f" chars, mean cost ${statistics.fmean(r['cost_usd'] for r in rs):.4f}")  # fmt: skip
    print("\nshare of the arm's total cost, by token kind (total $)")
    for arm in arms:
        rs = [r for r in rows if r["arm"] == arm]
        tot = sum(r["cost_usd"] for r in rs)
        parts = ", ".join(f"{k} {sum(r[f'usd_{k}'] for r in rs) / tot:.0%}" for k in KINDS)
        print(f"{arm:5} ${tot:7.2f}: {parts}")
    print("\ntool output characters, total by tool")
    for arm in arms:
        tot: dict[str, int] = {}
        for r in rows:
            if r["arm"] == arm:
                for k, v in json.loads(r["tool_output_chars_by_tool"]).items():
                    tot[k] = tot.get(k, 0) + v
        print(f"{arm:5} " + ", ".join(f"{k} {v:,}" for k, v in sorted(tot.items())))
    print("\nmechanism: runs passing each jobd_list argument; calls refused for a bad name")
    for arm in arms:
        rs = [r for r in rows if r["arm"] == arm]
        args = ", ".join(f"{k} {sum(r[f'list_{k}'] for r in rs)}" for k in LIST_ARGS)
        print(f"{arm:5} of {len(rs)}: {args}; refused {sum(r['refused_bad_name'] for r in rs)}")
    if len(sys.argv) > 2:
        with open(sys.argv[2], "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter="\t")
            w.writeheader()
            w.writerows(rows)


if __name__ == "__main__":
    main()
