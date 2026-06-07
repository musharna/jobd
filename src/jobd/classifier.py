"""Command classifier: decides whether a shell command is heavy."""

from __future__ import annotations

import re

from jobd.config import ClassifierRule
from jobd.models import ClassifyResult


def classify(cmd: str, rules: list[ClassifierRule]) -> ClassifyResult:
    """Return the first matching rule verdict, or heavy=False if nothing matches."""
    for rule in rules:
        for pattern in rule.match_regexes:
            if re.search(pattern, cmd):
                return ClassifyResult(
                    heavy=True,
                    rule_id=rule.id,
                    suggest_profile=rule.suggest_profile,
                    confidence=rule.confidence,
                    reason=f"matched regex: {pattern}",
                )
        for substr in rule.match_contains:
            if substr in cmd:
                return ClassifyResult(
                    heavy=True,
                    rule_id=rule.id,
                    suggest_profile=rule.suggest_profile,
                    confidence=rule.confidence,
                    reason=f"contains: {substr}",
                )
    return ClassifyResult(heavy=False)
