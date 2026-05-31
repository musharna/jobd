"""Tests for jobd.ctest_eta — opt-in ctest-cost-data ETA predictor."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jobd.ctest_eta import (
    CtestPrediction,
    _extract_regex,
    _find_cost_file,
    _parse_cost_file,
    predict_ctest,
)


@pytest.fixture
def ctest_on(monkeypatch):
    monkeypatch.setenv("JOBD_CTEST_PARSE", "1")


@pytest.fixture
def cost_dir(tmp_path: Path) -> Path:
    """Builds <tmp>/build-wsl/Testing/Temporary/CTestCostData.txt with a
    realistic CMake-formatted body (entries + '---' separator + metadata)."""
    d = tmp_path / "build-wsl" / "Testing" / "Temporary"
    d.mkdir(parents=True)
    (d / "CTestCostData.txt").write_text(
        "FooTest.AlphaCheck 10 1.5\n"
        "FooTest.BetaCheck 10 2.5\n"
        "BarTest.GammaCheck 7 0.5\n"
        "NeverRanTest.Skipped 0 0\n"
        "---\n"
        "FooTest.AlphaCheck\n"
        "FooTest.BetaCheck\n"
    )
    return tmp_path


def test_extract_regex_split_form():
    assert _extract_regex(["ctest", "-R", "FooTest.*"]) == "FooTest.*"


def test_extract_regex_joined_form():
    assert _extract_regex(["ctest", "-RFooTest.*"]) == "FooTest.*"


def test_extract_regex_no_R_flag_returns_none():
    assert _extract_regex(["ctest", "--output-on-failure"]) is None


def test_extract_regex_dangling_R_returns_none():
    assert _extract_regex(["ctest", "-R"]) is None


def test_find_cost_file_picks_freshest_build_dir(tmp_path: Path):
    old = tmp_path / "build-old" / "Testing" / "Temporary"
    new = tmp_path / "build-wsl" / "Testing" / "Temporary"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    old_file = old / "CTestCostData.txt"
    new_file = new / "CTestCostData.txt"
    old_file.write_text("Old 1 1.0\n")
    new_file.write_text("New 1 2.0\n")
    os.utime(old_file, (1000, 1000))
    os.utime(new_file, (2000, 2000))
    picked = _find_cost_file(str(tmp_path))
    assert picked == new_file


def test_find_cost_file_missing_returns_none(tmp_path: Path):
    assert _find_cost_file(str(tmp_path)) is None


def test_find_cost_file_cwd_not_a_dir(tmp_path: Path):
    f = tmp_path / "not-a-dir"
    f.write_text("hi")
    assert _find_cost_file(str(f)) is None


def test_parse_cost_file_stops_at_separator(cost_dir: Path):
    path = cost_dir / "build-wsl" / "Testing" / "Temporary" / "CTestCostData.txt"
    rows = _parse_cost_file(path)
    names = [r[0] for r in rows]
    assert names == [
        "FooTest.AlphaCheck",
        "FooTest.BetaCheck",
        "BarTest.GammaCheck",
        "NeverRanTest.Skipped",
    ]


def test_parse_cost_file_skips_malformed(tmp_path: Path):
    p = tmp_path / "c.txt"
    p.write_text("good 1 1.0\nshort_line\nbad_float 1 not_a_number\nokay 2 2.5\n")
    rows = _parse_cost_file(p)
    assert rows == [("good", 1, 1.0), ("okay", 2, 2.5)]


def test_predict_ctest_disabled_when_env_unset(monkeypatch, cost_dir: Path):
    monkeypatch.delenv("JOBD_CTEST_PARSE", raising=False)
    assert predict_ctest(["ctest", "-R", "FooTest.*"], str(cost_dir)) is None


def test_predict_ctest_disabled_when_env_zero(monkeypatch, cost_dir: Path):
    monkeypatch.setenv("JOBD_CTEST_PARSE", "0")
    assert predict_ctest(["ctest", "-R", "FooTest.*"], str(cost_dir)) is None


def test_predict_ctest_sum_matching_costs(ctest_on, cost_dir: Path):
    pred = predict_ctest(["ctest", "-R", "FooTest.*"], str(cost_dir))
    assert pred is not None
    assert pred.n_tests == 2
    assert pred.sum_cost_s == pytest.approx(4.0)
    assert pred.basis == "ctest-cost-K=2"


def test_predict_ctest_joined_R_form(ctest_on, cost_dir: Path):
    pred = predict_ctest(["ctest", "-RFooTest.*"], str(cost_dir))
    assert pred is not None
    assert pred.n_tests == 2


def test_predict_ctest_skips_zero_count_entries(ctest_on, cost_dir: Path):
    """NeverRanTest.Skipped has count=0, must not appear in matches even if regex catches it."""
    pred = predict_ctest(["ctest", "-R", "NeverRan"], str(cost_dir))
    assert pred is None


def test_predict_ctest_regex_no_match_returns_none(ctest_on, cost_dir: Path):
    assert predict_ctest(["ctest", "-R", "NotARealTestPattern"], str(cost_dir)) is None


def test_predict_ctest_invalid_regex_returns_none(ctest_on, cost_dir: Path):
    assert predict_ctest(["ctest", "-R", "[unclosed"], str(cost_dir)) is None


def test_predict_ctest_no_R_flag_returns_none(ctest_on, cost_dir: Path):
    assert predict_ctest(["ctest", "--output-on-failure"], str(cost_dir)) is None


def test_predict_ctest_wrong_head_returns_none(ctest_on, cost_dir: Path):
    assert predict_ctest(["pytest", "-R", "FooTest.*"], str(cost_dir)) is None


def test_predict_ctest_empty_cmd_returns_none(ctest_on, tmp_path: Path):
    assert predict_ctest([], str(tmp_path)) is None


def test_predict_ctest_no_cwd_returns_none(ctest_on):
    assert predict_ctest(["ctest", "-R", "Foo"], None) is None


def test_predict_ctest_no_cost_file_returns_none(ctest_on, tmp_path: Path):
    """cwd exists but no build dir → None (fall through to history)."""
    assert predict_ctest(["ctest", "-R", "Foo"], str(tmp_path)) is None


def test_predict_ctest_absolute_path_head(ctest_on, cost_dir: Path):
    """`Path(cmd[0]).name` resolves `/usr/bin/ctest` to `ctest`."""
    pred = predict_ctest(["/usr/bin/ctest", "-R", "FooTest.*"], str(cost_dir))
    assert pred is not None
    assert pred.n_tests == 2


def test_predict_ctest_basis_format(ctest_on, cost_dir: Path):
    pred = predict_ctest(["ctest", "-R", "FooTest.*"], str(cost_dir))
    assert pred is not None
    assert pred.basis.startswith("ctest-cost-K=")


def test_ctest_prediction_dataclass_immutable():
    p = CtestPrediction(sum_cost_s=10.0, n_tests=3)
    with pytest.raises((AttributeError, Exception)):
        p.sum_cost_s = 99.0  # type: ignore[misc]
