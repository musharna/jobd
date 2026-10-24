"""The 2026-10-20 model-tier verdict (PREREG_2026-10-20_model-tiers.md).

Reads each tier's ``scored.jsonl`` (written by ``analyze.py``), takes the
primary test per tier from ``analyze.win_rule`` (questions right in all 3
runs, mcp against alt, exact McNemar, bootstrap CI), adjusts the McNemar p
values by Holm across the tiers, and applies the pre-registered rule:

- a tier shows a **lift** if its Holm-adjusted p < 0.05, more questions only
  mcp got right, and the CI of the difference lies wholly above 0;
- a **loss** is the same with alt ahead and the CI wholly below 0;
- **WIN** = a lift at one or more tiers and a loss at none; **LOSS** = a loss
  at one or more tiers and a lift at none; **MIXED** = both; otherwise **TIE**.

    python benchmarks/agent_qa/tiers.py --key KEY --tier haiku=PATH --tier sonnet=PATH
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyze import win_rule

ALPHA = 0.05


def holm(ps: dict[str, float]) -> dict[str, float]:
    """Holm step-down adjusted p values (monotone, capped at 1)."""
    order = sorted(ps, key=lambda k: ps[k])
    out, running = {}, 0.0
    for i, k in enumerate(order):
        running = max(running, min(1.0, (len(ps) - i) * ps[k]))
        out[k] = running
    return out


def verdict(tiers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """``tiers``: name -> ``win_rule`` output. Returns the per-tier readout
    with Holm-adjusted p, and the server's verdict."""
    adj = holm({t: r["mcnemar_p"] for t, r in tiers.items()})
    per = {}
    for t, r in tiers.items():
        lift = adj[t] < ALPHA and r["only_mcp"] > r["only_alt"] and r["diff_lo95"] > 0
        loss = adj[t] < ALPHA and r["only_alt"] > r["only_mcp"] and r["diff_hi95"] < 0
        per[t] = {**r, "holm_p": adj[t], "lift": lift, "loss": loss}
    lifts = any(v["lift"] for v in per.values())
    losses = any(v["loss"] for v in per.values())
    v = "MIXED" if lifts and losses else "WIN" if lifts else "LOSS" if losses else "TIE"
    return {"verdict": v, "tiers": per}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--tier", action="append", required=True, help="name=path/to/scored.jsonl")
    ap.add_argument("--reps", type=int, default=3)
    a = ap.parse_args()
    qids = [q["id"] for q in json.loads(Path(a.key).read_text())["questions"]]
    tiers = {}
    for spec in a.tier:
        name, path = spec.split("=", 1)
        rows = [json.loads(ln) for ln in Path(path).read_text().splitlines()]
        tiers[name] = win_rule([r for r in rows if not r.get("baseline")], qids, a.reps)
    out = verdict(tiers)
    for t, r in out["tiers"].items():
        print(f"{t}: mcp {r['mcp_right_all']} vs alt {r['alt_right_all']} of {r['questions']}; "
              f"only mcp {r['only_mcp']}, only alt {r['only_alt']}; p {r['mcnemar_p']:.4g}, "
              f"Holm p {r['holm_p']:.4g}; CI {r['diff_lo95']:+.3f} to {r['diff_hi95']:+.3f}; "
              f"lift {r['lift']}, loss {r['loss']}")  # fmt: skip
    print("VERDICT:", out["verdict"])


if __name__ == "__main__":
    main()
