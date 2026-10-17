"""Controls for the agent benchmark that run without a model or a broker.

The real key is live broker data and stays out of the repo, so these use a
synthetic key in its shape. Each check pairs the outcome it guards against
with the one it must allow, in the same test.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks" / "agent_qa"))

from analyze import baseline_rows, invalid_reason, leak_hits  # noqa: E402
from score import oracle_reply, score, wrong_reply  # noqa: E402

KNOWN = {"alpha": "alpha", "alpha-wsl": "alpha", "bravo": "bravo", "charlie": "charlie",
         "old-box": "OLD-BOX"}  # fmt: skip
TRUTHS = {
    "exit_code": [{"exit_code": 0}, {"exit_code": 0}, {"exit_code": -15}],
    "host": [{"names": ["alpha", "alpha-wsl"], "known_names": KNOWN},
             {"names": ["OLD-BOX"], "known_names": KNOWN}],
    "wall_time": [{"seconds": 1214.912}, {"seconds": 3.2}],
    "project_failures": [{"job_ids": [3652, 3657]}, {"job_ids": [9]}],
    "log_line": [{"line": '{"rc":0,"model":"m-1"}'}, {"line": "== 3 passed in 0.1s =="}],
    "fleet": [{"hosts": ["alpha", "charlie"], "known_names": KNOWN},
              {"hosts": ["bravo"], "known_names": KNOWN}],
}  # fmt: skip
KEY = [
    {"id": f"q{i:02d}", "category": c, "truth": t}
    for i, (c, t) in enumerate(((c, t) for c, ts in TRUTHS.items() for t in ts), 1)
]


def _share_right(replies: list[str]) -> float:
    return sum(score(q, r)["verdict"] == "right" for q, r in zip(KEY, replies, strict=True)) / len(
        KEY
    )


def test_oracle_replies_score_100_percent_and_near_misses_0_percent() -> None:
    assert _share_right([oracle_reply(q) for q in KEY]) == 1.0
    assert _share_right([wrong_reply(q) for q in KEY]) == 0.0
    # Every near miss is a claim the scorer judged wrong, not a decline.
    assert {score(q, wrong_reply(q))["verdict"] for q in KEY} == {"wrong"}


def test_the_pre_sweep_oracle_check_passes_a_sound_scorer_and_stops_a_broken_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import run

    run.oracle_check(KEY)  # positive control: the real scorer on a sound key goes through
    lenient = lambda q, reply: {**score(q, reply), "verdict": "right"}  # noqa: E731
    monkeypatch.setattr(run, "score", lenient)
    with pytest.raises(SystemExit, match="oracle check failed"):
        run.oracle_check(KEY)  # every near miss passes: too lenient to tell
    strict = lambda q, reply: {**score(q, reply), "verdict": "wrong"}  # noqa: E731
    monkeypatch.setattr(run, "score", strict)
    with pytest.raises(SystemExit, match="oracle check failed"):
        run.oracle_check(KEY)  # the key's own answers fail: cannot recognise a hit


def _transcript(tmp_path: Path, tool_input: str, tool_output: str) -> Path:
    t = tmp_path / "q01-1.jsonl"
    events = [
        {"type": "system", "subtype": "init", "cwd": "/tmp/agentqa-alt-x/cwd"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": tool_input}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": [{"type": "text", "text": tool_output}]}]}},
        {"type": "user", "message": "a bare string message"},
    ]  # fmt: skip
    t.write_text("".join(json.dumps(e) + "\n" for e in events))
    return t


@pytest.mark.parametrize(
    ("tool_input", "tool_output"),
    [
        ("cat ~/jobd-artifacts/agent_qa/questions.json", "permission denied"),
        ("job status 12", "cwd: /x/jobd/.claude/worktrees/bench-1016/benchmarks/agent_qa"),
        ("job logs 5", "see https://github.com/musharna/jobd/tree/main/benchmarks/agent_qa"),
        ("ls", "results.jsonl  runs"),
    ],
)
def test_the_leak_scan_flags_the_key_and_benchmark_paths_in_inputs_or_outputs(
    tmp_path: Path, tool_input: str, tool_output: str
) -> None:
    assert leak_hits(_transcript(tmp_path, tool_input, tool_output))
    # Positive control: an ordinary lookup, whose job record names another
    # repo's worktree (jobs really run there), is not a leak.
    clean = _transcript(
        tmp_path, "job status 1711", "exit_code=0\ncwd: /x/agrigen/.claude/worktrees/roots/backend"
    )
    assert leak_hits(clean) == []


def _row(arm: str, **kw: object) -> dict:
    tools = {"bare": [], "alt": ["Bash"], "mcp": sorted(
        f"mcp__jobd__jobd_{t}" for t in ("events", "list", "logs", "status", "submit", "workers")
    )}[arm]  # fmt: skip
    servers = [{"name": "jobd", "status": "connected"}] if arm == "mcp" else []
    calls = [] if arm == "bare" else [tools[0]]
    row = {"arm": arm, "exit": 0, "cost_usd": 0.02, "tools_available": tools,
           "mcp_servers": servers, "tool_calls": calls}  # fmt: skip
    return {**row, **kw}


def test_one_validity_rule_for_every_arm() -> None:
    for arm in ("bare", "alt", "mcp"):
        assert invalid_reason(_row(arm), [], True) is None  # positive control
        assert invalid_reason(_row(arm, exit="timeout"), [], True) == "exit timeout"
        assert invalid_reason(_row(arm, cost_usd=None), [], True) == "no result event"
        assert invalid_reason(_row(arm), ["questions.json"], True) == "leak: questions.json"
    assert invalid_reason(_row("alt", tool_calls=[]), [], True) == "no tool call"
    assert invalid_reason(_row("mcp", tool_calls=[]), [], True) == "no tool call"
    assert invalid_reason(_row("bare", tools_available=["Bash"]), [], True) == "wrong tools"
    failed = _row("mcp", mcp_servers=[{"name": "jobd", "status": "failed"}])
    assert invalid_reason(failed, [], True) == "wrong tools"
    offered = _row(
        "mcp", tools_available=[*_row("mcp")["tools_available"], "mcp__jobd__jobd_cancel"]
    )
    assert invalid_reason(offered, [], True) == "mutating tool offered"


def test_the_best_constant_baseline_takes_the_modal_answer_per_kind() -> None:
    fleet = [{"host": "alpha", "state": "online"}, {"host": "bravo", "state": "offline"}]
    rows = baseline_rows(KEY, fleet)
    best = {r["qid"]: r["verdict"] for r in rows if r["arm"] == "best_constant"}
    prior = {r["qid"]: r["verdict"] for r in rows if r["arm"] == "prior"}
    # Exit code 0 is the key's mode (2 of 3): right twice, wrong once.
    assert [best[q] for q in ("q01", "q02", "q03")] == ["right", "right", "wrong"]
    # One constant per kind: never more than one host question right.
    assert sum(best[q] == "right" for q in ("q04", "q05")) == 1
    assert sum(v == "right" for v in best.values()) < len(KEY)
    # The prior row guesses exit 0 and every online worker, without the key.
    assert prior["q01"] == "right" and prior["q03"] == "wrong"
