"""The agent benchmark's scorer: one reply, one verdict, decided by its ANSWER line.

Each test pairs the wrong or declined reply with the right one for the same
question, so a scorer that called everything right (or wrong) fails here.
The questions are hand-made in the key's shape, with neutral names.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks" / "agent_qa"))

from build_questions import eligible, gpu_tier_gb, host_names, last_line  # noqa: E402
from score import extract, score  # noqa: E402

KNOWN = {"alpha": "alpha", "alpha-wsl": "alpha", "bravo": "bravo", "charlie": "charlie",
         "old-box": "OLD-BOX"}  # fmt: skip
EXIT = {"category": "exit_code", "truth": {"exit_code": -15}}
HOST = {"category": "host", "truth": {"names": ["alpha", "alpha-wsl"], "known_names": KNOWN}}
WALL = {"category": "wall_time", "truth": {"seconds": 1214.912}}
FAILS = {"category": "project_failures", "truth": {"job_ids": [3652, 3657, 3658]}}
LOG = {"category": "log_line", "truth": {"line": '{"rc":0,"model":"m-1"}'}}
FLEET = {"category": "fleet", "truth": {"hosts": ["alpha", "charlie"], "known_names": KNOWN}}


def _v(q: dict, reply: str) -> str:
    return score(q, reply)["verdict"]


def test_the_last_answer_line_decides_and_markdown_around_it_is_ignored() -> None:
    reply = "ANSWER: 1\nChecking that again...\n**ANSWER:** -15"
    assert extract(reply) == "-15"
    assert _v(EXIT, reply) == "right"
    assert _v(EXIT, "**ANSWER:** 1") == "wrong"


def test_unknown_is_declined_and_a_reply_without_the_line_is_neither() -> None:
    assert _v(EXIT, "No access.\nANSWER: UNKNOWN") == "declined"
    assert _v(EXIT, "ANSWER: `UNKNOWN`") == "declined"
    assert _v(EXIT, "It exited with -15.") == "no_answer"
    assert _v(EXIT, "It exited with -15.\nANSWER: -15") == "right"


def test_exit_code_keeps_its_sign() -> None:
    """jobd records a SIGTERM'd job as -15; 143 (128+15) is another number."""
    assert _v(EXIT, "ANSWER: -15") == "right"
    assert _v(EXIT, "ANSWER: -15 (SIGTERM)") == "right"
    assert _v(EXIT, "ANSWER: 15") == "wrong"
    assert _v(EXIT, "ANSWER: 143") == "wrong"


def test_host_accepts_any_name_of_the_host_and_flags_a_name_no_host_has() -> None:
    assert _v(HOST, "ANSWER: alpha") == "right"
    assert _v(HOST, "ANSWER: Alpha-WSL") == "right"
    assert _v(HOST, "ANSWER: alpha (the big GPU box)") == "right"
    assert _v(HOST, "ANSWER: bravo") == "wrong"
    assert _v(HOST, "ANSWER: alpha, bravo") == "wrong"  # one host ran it
    assert score(HOST, "ANSWER: bravo")["unknown_host"] is False  # wrong, but a real host
    assert score(HOST, "ANSWER: zulu")["unknown_host"] is True
    assert score(HOST, "ANSWER: alpha")["unknown_host"] is False  # right, so not flagged


def test_wall_time_is_within_one_second() -> None:
    assert _v(WALL, "ANSWER: 1215") == "right"
    assert _v(WALL, "ANSWER: 1214") == "right"  # truncated
    assert _v(WALL, "ANSWER: 1,215 s") == "right"
    assert _v(WALL, "ANSWER: 0:20:15") == "right"
    assert _v(WALL, "ANSWER: 1216") == "wrong"
    assert _v(WALL, "ANSWER: 1213") == "wrong"
    assert _v(WALL, "ANSWER: about 20 minutes") == "wrong"


def test_project_failures_needs_the_exact_set_in_any_order() -> None:
    assert _v(FAILS, "ANSWER: 3658, 3652, 3657") == "right"
    assert _v(FAILS, "ANSWER: #3652, #3657 and #3658") == "right"
    assert _v(FAILS, "ANSWER: 3652, 3657") == "wrong"  # one missing
    assert _v(FAILS, "ANSWER: 3652, 3657, 3658, 3660") == "wrong"  # one extra
    assert _v(FAILS, "ANSWER: none") == "wrong"


def test_a_log_line_is_a_quotation_up_to_whitespace_and_wrapping_quotes() -> None:
    assert _v(LOG, 'ANSWER: {"rc":0,"model":"m-1"}') == "right"
    assert _v(LOG, 'ANSWER: `{"rc":0,"model":"m-1"}`') == "right"
    assert _v(LOG, 'ANSWER: {"rc":0,"model":"M-1"}') == "wrong"  # case counts
    assert _v(LOG, 'ANSWER: {"rc":0}') == "wrong"
    plain = {"category": "log_line", "truth": {"line": "2 failed, 49 deselected in 3.14s"}}
    assert _v(plain, 'ANSWER: "2 failed,  49 deselected in 3.14s"') == "right"
    assert _v(plain, "ANSWER: 2 failed, 49 deselected") == "wrong"


def test_fleet_compares_host_sets_through_the_alias_table() -> None:
    assert _v(FLEET, "ANSWER: charlie, alpha") == "right"
    assert _v(FLEET, "ANSWER: alpha-wsl and charlie") == "right"
    assert _v(FLEET, "ANSWER: alpha") == "wrong"
    assert _v(FLEET, "ANSWER: alpha, bravo, charlie") == "wrong"
    assert score(FLEET, "ANSWER: alpha, zulu")["unknown_host"] is True


def test_the_key_builders_rules_on_worker_dicts() -> None:
    """The routing rule the fleet answers come from, and the alias and
    log-line rules, on hand-made inputs (the builder cross-checks the
    routing answers against the broker's own dry run when it runs)."""
    ws = [
        {"host": "alpha", "state": "online", "host_aliases": ["any", "any-gpu", "alpha-wsl"],
         "tags": ["R", "cuda", "cuda-32gb"]},
        {"host": "bravo", "state": "online", "host_aliases": ["any"], "tags": ["R", "cuda-8gb"]},
        {"host": "charlie", "state": "offline", "host_aliases": ["any"], "tags": ["R"]},
    ]  # fmt: skip
    assert eligible(["R"], ws) == ["alpha", "bravo"]  # charlie is offline
    assert eligible(["R", "cuda"], ws) == ["alpha"]
    assert eligible(["ffmpeg"], ws) == []
    assert host_names("alpha", ws) == ["alpha", "alpha-wsl"]  # pool aliases name no host
    assert host_names("retired", ws) == ["retired"]
    assert [gpu_tier_gb(w) for w in ws] == [32, 8, 0]
    assert last_line("a\nb line here\n\n") == "b line here"
    assert last_line("step 1\rstep 2 of many\n") is None  # a progress bar
    assert last_line("\x1b[31mred text here\x1b[0m\n") is None
    assert last_line("ok\n") is None  # too short to quote
