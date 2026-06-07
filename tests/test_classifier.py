"""Tests for the classifier rule engine."""

from pathlib import Path

import yaml

from jobd.classifier import classify
from jobd.config import ClassifierRule


def rule(id, regex, profile="small", confidence="high"):
    return ClassifierRule(
        id=id,
        match_regexes=[regex],
        match_contains=[],
        suggest_profile=profile,
        confidence=confidence,
    )


def test_classify_no_match():
    rules = [rule("x", r"^frobnicate")]
    result = classify("ls -la", rules)
    assert result.heavy is False


def test_classify_regex_match():
    rules = [rule("sdxl", r"^(bash\s+)?train_lora_v\d+\.sh\b", profile="gpu-heavy")]
    result = classify("bash train_lora_v5.sh --config x.yaml", rules)
    assert result.heavy is True
    assert result.rule_id == "sdxl"
    assert result.suggest_profile == "gpu-heavy"
    assert result.confidence == "high"


def test_classify_contains_match():
    r = ClassifierRule(
        id="stylegan",
        match_regexes=[],
        match_contains=["stylegan2"],
        suggest_profile="gpu-heavy",
        confidence="high",
    )
    result = classify("python train.py --cfg stylegan2-ada", [r])
    assert result.heavy is True
    assert result.rule_id == "stylegan"


def test_classify_first_match_wins():
    rules = [
        rule("first", r"^bash ", confidence="medium"),
        rule("second", r"^bash train_lora"),
    ]
    result = classify("bash train_lora_v5.sh", rules)
    assert result.rule_id == "first"
    assert result.confidence == "medium"


def test_classify_negative_not_heavy():
    rules = [rule("sdxl", r"^(bash\s+)?train_lora_v\d+\.sh\b")]
    result = classify("ls train_lora_v5.sh", rules)
    assert result.heavy is False


def test_classifier_fixtures():
    """Smoke test all fixture cases against their rules."""
    fixtures_path = Path(__file__).parent / "fixtures" / "classifier_cases.yaml"
    cases = yaml.safe_load(fixtures_path.read_text())["cases"]

    rule_map = {
        "sdxl-lora-train": rule("sdxl-lora-train", r"^(bash\s+)?train_lora_v\d+\.sh\b"),
    }
    for case in cases:
        r = rule_map[case["rule_id"]]
        for pos_cmd in case["positive"]:
            assert classify(pos_cmd, [r]).heavy, f"Expected heavy: {pos_cmd}"
        for neg_cmd in case["negative"]:
            assert not classify(neg_cmd, [r]).heavy, f"Expected not heavy: {neg_cmd}"
