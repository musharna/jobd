"""The 2026-10-20 model-tier re-run's harness additions: --model (paths and
the served-model check), --accept-broker, the substitute comparison and the
mechanism readout. No model, no broker."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks" / "agent_qa"))

import run  # noqa: E402
from analyze import mechanism, substitute  # noqa: E402

HAIKU = "claude-haiku-5-5"


def test_model_paths_keep_the_default_models_and_separate_any_other() -> None:
    runs, results = run.model_paths(run.DEFAULT_MODEL)
    assert (runs.name, results.name) == ("runs", "results.jsonl")
    runs, results = run.model_paths(HAIKU)
    assert (runs.name, results.name) == (f"runs-{HAIKU}", f"results-{HAIKU}.jsonl")
    assert runs.parent == results.parent == run.ARTIFACTS


def test_check_broker_accepts_the_keys_version_or_the_one_named() -> None:
    assert run.check_broker("0.5.51", "0.5.51", None) is None
    assert run.check_broker("0.5.52", "0.5.51", "0.5.52") is None
    assert "built on 0.5.51" in run.check_broker("0.5.52", "0.5.51", None)
    # --accept-broker names one version, not "any".
    assert "names 0.5.53" in run.check_broker("0.5.52", "0.5.51", "0.5.53")


class _Proxy:
    url = "http://127.0.0.1:1"

    def __init__(self, *a, **k) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def _fake_claude(served: str):
    events = [
        {"type": "system", "subtype": "init", "model": served, "tools": [],
         "claude_code_version": "2.1.305", "mcp_servers": []},
        {"type": "result", "subtype": "success", "result": "ANSWER: 13", "total_cost_usd": 0.001},
    ]  # fmt: skip

    def fake_run(cmd, **kw):
        assert cmd[cmd.index("--model") + 1] == run.MODEL
        return SimpleNamespace(stdout="\n".join(map(json.dumps, events)), returncode=0)

    return fake_run


@pytest.mark.parametrize("served", [HAIKU, "claude-opus-5-5"])
def test_run_one_records_the_served_model_and_stops_on_another(
    served: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run, "ReadOnlyProxy", _Proxy)
    monkeypatch.setattr(subprocess, "run", _fake_claude(served))
    monkeypatch.setattr(run, "MODEL", run.MODEL)  # restored after the test
    run.set_model(HAIKU)
    q = {"id": "a", "category": "window_count", "question": "?", "answer_format": "an integer",
         "truth": {"count": 13}}  # fmt: skip
    o = run.Opts(tmp_path / "runs", run.SERVER, None)
    if served == HAIKU:
        row = run.run_one("bare", q, 1, "http://x", "", o)
        assert row["model"] == HAIKU and row["verdict"] == "right"
    else:
        with pytest.raises(run.WrongModel, match="served by claude-opus-5-5"):
            run.run_one("bare", q, 1, "http://x", "", o)


def _rows(arm: str, right: list[str], qids: list[str], **extra) -> list[dict]:
    return [{"arm": arm, "qid": q, "rep": k, "valid": True,
             "verdict": "right" if q in right else "wrong", **extra}
            for q in qids for k in (1, 2, 3)]  # fmt: skip


def test_substitute_pairs_this_mcp_arm_with_the_reference_alt_arm() -> None:
    qids = [f"h{i:02d}" for i in range(38)]
    ours = _rows("mcp", qids[:30], qids) + _rows("alt", qids[:5], qids)
    ref = _rows("alt", qids[:37], qids) + _rows("mcp", qids, qids)
    ref.append({"arm": "alt", "qid": "h00", "rep": 1, "baseline": True, "valid": True,
                "verdict": "wrong"})  # fmt: skip
    out = substitute(ours, ref, qids)
    # Our alt and their mcp play no part; the baseline row is ignored.
    assert (out["mcp_right_all"], out["alt_right_all"]) == (30, 37)
    assert (out["only_mcp"], out["only_alt"]) == (0, 7) and out["mcnemar_p"] < 0.05


def test_mechanism_counts_server_calls_and_runs_without_one() -> None:
    rows = [
        {"arm": "mcp", "tool_calls": ["mcp__jobd__jobd_list", "mcp__jobd__jobd_logs"]},
        {"arm": "mcp", "tool_calls": []},
        # Any other tool the client might expose is not a server call.
        {"arm": "mcp", "tool_calls": ["mcp__jobd__jobd_list", "ToolSearch"]},
        {"arm": "alt", "tool_calls": ["Bash"]},
    ]
    assert mechanism(rows) == {"runs": 3, "server_calls_mean": 1.0, "runs_with_no_server_call": 1}


def _tier(p: float, only_mcp: int, only_alt: int, lo: float, hi: float) -> dict:
    return {"mcnemar_p": p, "only_mcp": only_mcp, "only_alt": only_alt,
            "diff_lo95": lo, "diff_hi95": hi}  # fmt: skip


def test_holm_steps_down_and_stays_monotone() -> None:
    from tiers import holm

    assert holm({"a": 0.01, "b": 0.04}) == {"a": 0.02, "b": 0.04}
    assert holm({"a": 0.03, "b": 0.02}) == {"b": 0.04, "a": 0.04}
    assert holm({"a": 0.6, "b": 0.7}) == {"a": 1.0, "b": 1.0}


def test_tier_verdict_win_loss_mixed_tie() -> None:
    from tiers import verdict

    lift, loss = _tier(0.001, 9, 0, 0.1, 0.4), _tier(0.001, 0, 9, -0.4, -0.1)
    null = _tier(1.0, 1, 1, -0.05, 0.05)
    assert verdict({"haiku": lift, "sonnet": null})["verdict"] == "WIN"
    assert verdict({"haiku": null, "sonnet": loss})["verdict"] == "LOSS"
    assert verdict({"haiku": lift, "sonnet": loss})["verdict"] == "MIXED"
    assert verdict({"haiku": null, "sonnet": null})["verdict"] == "TIE"
    # p = 0.03 on one tier is significant raw but not after Holm (0.03 x 2).
    weak = _tier(0.03, 5, 0, 0.02, 0.2)
    out = verdict({"haiku": weak, "sonnet": null})
    assert out["verdict"] == "TIE" and out["tiers"]["haiku"]["holm_p"] == pytest.approx(0.06)
    # Significant, but the CI touches 0: not a lift.
    assert verdict({"haiku": _tier(0.001, 9, 0, 0.0, 0.3), "sonnet": null})["verdict"] == "TIE"


def test_stop_at_moves_the_gates_seven_day_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run, "STOP_AT", run.STOP_AT)  # restored after the test
    rl = {"status": "allowed", "five_hour": 0.1, "five_hour_resets": 0, "seven_day": 0.85}
    with pytest.raises(run.UsageStop):
        run.Gate(tmp_path / "p.jsonl").update(rl, "x")
    run.set_stop_at(0.90)
    run.Gate(tmp_path / "p.jsonl").update(rl, "x")  # 0.85 < 0.90: no stop
    with pytest.raises(run.UsageStop):
        run.Gate(tmp_path / "p.jsonl").update({**rl, "seven_day": 0.90}, "x")


def test_spend_gate_stops_the_sweep_before_a_run_and_lets_it_through_under_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API-billed sweeps (2026-10-23): Gate.wait asks BENCH_SPEND_GATE before every run."""
    gate = tmp_path / "gate.py"
    gate.write_text("import sys, os\nprint('cap reached', file=sys.stderr)\nsys.exit(int(os.environ['GATE_RC']))\n")
    monkeypatch.setenv("BENCH_SPEND_GATE", str(gate))
    monkeypatch.setenv("GATE_RC", "3")
    with pytest.raises(run.UsageStop, match="cap reached"):
        run.Gate(tmp_path / "p.jsonl").wait()
    monkeypatch.setenv("GATE_RC", "0")
    run.Gate(tmp_path / "p.jsonl").wait()  # under the cap: no stop
    monkeypatch.delenv("BENCH_SPEND_GATE")
    monkeypatch.setenv("GATE_RC", "3")
    run.Gate(tmp_path / "p.jsonl").wait()  # unset: subscription sweeps unchanged
