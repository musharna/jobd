"""The 2026-10-18 re-run's harness: version-2 scoring, the usage gate, the
API-failure rule, the server spec, the win rule and the v2 drift check.
No model, no broker."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks" / "agent_qa"))

import build_hard  # noqa: E402
import run  # noqa: E402
from analyze import win_rule  # noqa: E402
from score import oracle_reply, score, wrong_reply  # noqa: E402

KNOWN = {"laptop": "laptop", "desktop": "desktop", "desktop-wsl": "desktop", "gt76": "gt76"}
V2 = [
    {"id": "a", "category": "window_count", "truth": {"count": 13}},
    {"id": "b", "category": "event_count", "truth": {"count": 7}},
    {"id": "c", "category": "window_ids", "truth": {"job_ids": [4424, 4433, 4508]}},
    {"id": "d", "category": "busiest_host",
     "truth": {"tally": {"desktop": 11, "laptop": 9}, "names": ["desktop", "desktop-wsl"],
               "known_names": KNOWN}},
    {"id": "e", "category": "busiest_project",
     "truth": {"tally": {"agrigen": 10, "asset-gen": 5}, "project": "agrigen"}},
    {"id": "f", "category": "total_runtime",
     "truth": {"seconds": 5103.342, "tolerance_s": 5.0, "job_ids": [1, 2, 3, 4, 5]}},
    {"id": "g", "category": "queue_wait", "truth": {"seconds": 2280.27}},
    {"id": "h", "category": "log_join",
     "truth": {"lines": {"6603": "x", "6676": "y"}, "job_id": 6676}},
]  # fmt: skip


def test_v2_oracle_replies_score_right_and_near_misses_wrong() -> None:
    for q in V2:
        assert score(q, oracle_reply(q))["verdict"] == "right", q["id"]
        assert score(q, wrong_reply(q))["verdict"] == "wrong", q["id"]


@pytest.mark.parametrize(
    ("qid", "reply", "verdict"),
    [
        ("d", "desktop-wsl", "right"),  # an alias of the top host
        ("d", "desktop, laptop", "wrong"),  # two hosts is not one answer
        ("e", "`Agrigen`", "right"),
        ("e", "agrigen-trellis", "wrong"),
        ("f", "5108", "right"),  # 4.66 s off, inside 1 s per job
        ("f", "5109", "wrong"),
        ("f", "1:25:03", "right"),
        ("g", "2281", "right"),
        ("g", "2282", "wrong"),
        ("h", "6676", "right"),
        ("h", "6603, 6676", "wrong"),
        ("c", "4508, 4424, 4433", "right"),
        ("c", "4424, 4433", "wrong"),
        ("a", "13 jobs", "right"),
    ],
)
def test_v2_matching_rules(qid: str, reply: str, verdict: str) -> None:
    q = next(q for q in V2 if q["id"] == qid)
    assert score(q, f"ANSWER: {reply}")["verdict"] == verdict


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def _rl(five: float, seven: float = 0.2, status: str = "allowed") -> dict:
    return {"status": status, "five_hour": five, "five_hour_resets": 1_003_600,
            "seven_day": seven, "seven_day_resets": 2_000_000}  # fmt: skip


def test_gate_pauses_at_085_until_the_reset_and_records_it(tmp_path: Path) -> None:
    c = Clock()
    g = run.Gate(tmp_path / "pauses.jsonl", sleep=c.sleep, now=c.now)
    g.update(_rl(0.84), "alt h01 rep1")  # positive control: below the line, no pause
    g.wait()
    assert c.slept == [] and not (tmp_path / "pauses.jsonl").exists()
    g.update(_rl(0.85), "mcp h01 rep1")
    g.wait()
    assert c.t == 1_003_660  # resetsAt + 60 s
    log = [json.loads(ln) for ln in (tmp_path / "pauses.jsonl").read_text().splitlines()]
    assert len(log) == 1 and log[0]["after"] == "mcp h01 rep1" and log[0]["five_hour"] == 0.85


def test_gate_pauses_on_a_rejection_and_stops_on_seven_day(tmp_path: Path) -> None:
    c = Clock()
    g = run.Gate(tmp_path / "p.jsonl", sleep=c.sleep, now=c.now)
    g.update(_rl(0.40, status="rejected"), "x")
    assert g.until == 1_003_660
    g.update(_rl(0.40, seven=0.79), "y")  # below the stop line
    with pytest.raises(run.UsageStop):
        g.update(_rl(0.40, seven=0.80), "z")


def _events(*, rejected: bool, result: dict | None) -> list[dict]:
    ev: list[dict] = [{"type": "system", "subtype": "init"}]
    if rejected:
        ev.append({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}})
    if result is not None:
        ev.append({"type": "result", **result})
    return ev


def test_api_failure_is_a_rejection_before_an_answer_and_nothing_else() -> None:
    ok = {"subtype": "success", "is_error": False, "result": "ANSWER: 3"}
    err = {"subtype": "success", "is_error": True, "result": "API Error: rate limit"}
    assert run.api_failure(_events(rejected=True, result=err), 0) == "rate_limited"
    assert run.api_failure(_events(rejected=True, result=None), 1) == "rate_limited"
    # A rejection the run recovered from, and a plain answer, are not failures.
    assert run.api_failure(_events(rejected=True, result=ok), 0) is None
    assert run.api_failure(_events(rejected=False, result=ok), 0) is None
    # A timeout stays in as an invalid run.
    assert run.api_failure(_events(rejected=False, result=None), "timeout") is None
    assert (
        run.api_failure(_events(rejected=False, result={"terminal_reason": "api_error"}), 0)
        == "api_error"
    )


def test_drop_api_failures_moves_each_triple_once(tmp_path: Path) -> None:
    res = tmp_path / "results.jsonl"
    rows = [
        {"arm": "alt", "qid": "h01", "rep": 1, "api_failure": "rate_limited"},
        {"arm": "mcp", "qid": "h01", "rep": 1, "api_failure": None},
    ]
    res.write_text("".join(json.dumps(r) + "\n" for r in rows))
    run.drop_api_failures(res)
    assert [r["arm"] for r in map(json.loads, res.read_text().splitlines())] == ["mcp"]
    # The re-run failed the same way: it stays this time.
    with res.open("a") as fh:
        fh.write(json.dumps(rows[0]) + "\n")
    run.drop_api_failures(res)
    assert len(res.read_text().splitlines()) == 2
    assert len(res.with_suffix(".api_failures.jsonl").read_text().splitlines()) == 1


def test_server_spec_takes_a_full_commit_only() -> None:
    sha = "d2817e4" + "0" * 33
    assert run.check_server_spec(f"git+https://github.com/musharna/jobd@{sha}") == sha
    assert run.check_server_spec(run.SERVER) is None
    for bad in ("git+https://github.com/musharna/jobd@d2817e4", "jobd[mcp]==0.5.50",
                "git+https://github.com/someone/jobd@" + sha):  # fmt: skip
        with pytest.raises(SystemExit):
            run.check_server_spec(bad)
    cmd = run.server_cmd(f"git+https://github.com/musharna/jobd@{sha}", "2026-10-18")
    assert cmd == ["uvx", "--exclude-newer", "2026-10-18", "--from",
                   f"jobd[mcp] @ git+https://github.com/musharna/jobd@{sha}", "jobd-mcp"]  # fmt: skip
    assert run.server_cmd(run.SERVER, None) == run.MCP_SERVER


def _rows(right: dict[str, list[str]], qids: list[str]) -> list[dict]:
    return [
        {"arm": a, "qid": q, "rep": k, "valid": True,
         "verdict": "right" if q in right[a] else "wrong"}
        for a in ("alt", "mcp") for q in qids for k in (1, 2, 3)
    ]  # fmt: skip


def test_win_rule_win_tie_loss() -> None:
    qids = [f"h{i:02d}" for i in range(38)]
    base = qids[:20]
    win = win_rule(_rows({"alt": base, "mcp": qids[:32]}, qids), qids)
    assert win["verdict"] == "WIN" and win["only_mcp"] == 12 and win["mcnemar_p"] < 0.05
    assert win_rule(_rows({"alt": base, "mcp": qids[:23]}, qids), qids)["verdict"] == "TIE"
    # Non-inferiority: 3 fewer is a loss even though p > 0.05.
    loss = win_rule(_rows({"alt": base, "mcp": qids[:17]}, qids), qids)
    assert loss["verdict"] == "LOSS" and loss["mcnemar_p"] > 0.05
    assert win_rule(_rows({"alt": base, "mcp": qids[:18]}, qids), qids)["verdict"] == "TIE"


def test_win_rule_counts_an_invalid_run_as_not_right() -> None:
    qids = ["h01"]
    rows = _rows({"alt": qids, "mcp": qids}, qids)
    rows[-1]["valid"] = False
    out = win_rule(rows, qids)
    assert out["mcp_right_all"] == 0 and out["alt_right_all"] == 1


def _job(i, project, state, fin, worker="laptop", start=None):
    return {"id": i, "project": project, "state": state, "worker": worker,
            "finished_at": fin, "started_at": start or fin, "submitted_at": fin}  # fmt: skip


def test_v2_truths_are_half_open_and_drift_sees_a_changed_answer() -> None:
    jobs = [
        _job(1, "p", "failed", "2026-10-01T00:00:00+00:00"),  # at the start: in
        _job(2, "p", "failed", "2026-10-01T06:00:00+00:00"),  # at the end: out
        _job(3, "q", "failed", "2026-10-01T03:00:00+00:00", worker="desktop"),
        _job(
            4, "p", "completed", "2026-10-01T02:00:00+00:00", start="2026-10-01T01:59:00+00:00"
        ),  # fmt: skip
    ]
    w = {"after": "2026-10-01T00:00:00Z", "before": "2026-10-01T06:00:00Z"}
    assert build_hard.t_window_ids(w, jobs, None) == {"job_ids": [1, 3]}
    assert build_hard.t_window_count({**w, "project": "p", "state": "failed"}, jobs, None) == {
        "count": 1
    }
    assert build_hard.t_busiest_project({**w, "state": "failed"}, jobs, None)["tally"] == {
        "p": 1, "q": 1,
    }  # fmt: skip
    assert build_hard.unique_top({"p": 1, "q": 1}) is None
    assert build_hard.t_total_runtime({**w, "project": "p"}, jobs, None)["seconds"] == 60.0

    key = {"questions": [{"id": "h01", "category": "window_ids", "params": w,
                          "truth": {"job_ids": [1, 3]}}]}  # fmt: skip

    class FakeBroker:
        def __init__(self, rows):
            self.rows = rows

        def get_with_total(self, path, **params):
            return self.rows, len(self.rows)

    assert build_hard.drift(FakeBroker(jobs), key) == []  # positive control
    moved = [*jobs[:2], {**jobs[2], "finished_at": "2026-10-01T07:00:00+00:00"}, jobs[3]]
    assert build_hard.drift(FakeBroker(moved), key) == ["h01 (window_ids): answer changed"]
