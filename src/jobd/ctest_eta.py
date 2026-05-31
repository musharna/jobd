"""ctest-aware ETA: opt-in sub-job parser for `ctest -R <regex>` submits.

When `JOBD_CTEST_PARSE=1` and a job's cmd is `ctest -R <regex>` and `<cwd>`
contains a `build*/Testing/Temporary/CTestCostData.txt`, sum the per-test
avg-cost values whose test names match the regex. Returns a `CtestPrediction`
that the broker surfaces as `eta_basis="ctest-cost-K=<N>"` so the CLI banner
shows it on the first run of a new regex (where history is ColdStart).

CTestCostData.txt format (CMake source: cmCTestMultiProcessHandler.cxx):
    <test_name> <prior_run_count> <avg_cost_seconds>
followed by a `---` separator and per-cmake metadata that we ignore.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CtestPrediction:
    sum_cost_s: float
    n_tests: int

    @property
    def basis(self) -> str:
        return f"ctest-cost-K={self.n_tests}"


def _find_cost_file(cwd: str) -> Path | None:
    """Pick freshest <cwd>/build*/Testing/Temporary/CTestCostData.txt."""
    cwd_p = Path(cwd)
    if not cwd_p.is_dir():
        return None
    candidates = list(cwd_p.glob("build*/Testing/Temporary/CTestCostData.txt"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _extract_regex(cmd: list[str]) -> str | None:
    """Return the -R regex from `ctest -R <regex>` or `ctest -R<regex>`.

    Returns None when no -R flag is present.
    """
    i = 0
    while i < len(cmd):
        tok = cmd[i]
        if tok == "-R" and i + 1 < len(cmd):
            return cmd[i + 1]
        if tok.startswith("-R") and len(tok) > 2:
            return tok[2:]
        i += 1
    return None


def _parse_cost_file(path: Path) -> list[tuple[str, int, float]]:
    """Parse CTestCostData.txt into (name, count, cost) rows.

    Stops at first `---` separator line. Skips malformed lines defensively.
    """
    rows: list[tuple[str, int, float]] = []
    try:
        text = path.read_text()
    except OSError:
        return rows
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("---"):
            break
        parts = stripped.split()
        if len(parts) < 3:
            continue
        try:
            count = int(parts[-2])
            cost = float(parts[-1])
        except ValueError:
            continue
        # name may contain spaces in pathological cases; rejoin everything
        # except the trailing two numeric tokens.
        name = " ".join(parts[:-2])
        rows.append((name, count, cost))
    return rows


def predict_ctest(cmd: list[str], cwd: str | None) -> CtestPrediction | None:
    """Return a ctest-based wall prediction, or None if not applicable.

    Gates (all must hold for a non-None return):
      - JOBD_CTEST_PARSE=1 in env
      - cmd[0] basename is `ctest`
      - cmd contains `-R <regex>` (split or joined form)
      - cwd resolves; build*/Testing/Temporary/CTestCostData.txt exists
      - >=1 test in the cost file with count>0 matches the regex
    """
    if os.environ.get("JOBD_CTEST_PARSE") != "1":
        return None
    if not cmd or not cwd:
        return None
    if Path(cmd[0]).name != "ctest":
        return None
    regex_str = _extract_regex(cmd)
    if regex_str is None:
        return None
    try:
        regex = re.compile(regex_str)
    except re.error:
        return None
    cost_file = _find_cost_file(cwd)
    if cost_file is None:
        return None
    rows = _parse_cost_file(cost_file)
    matches = [cost for name, count, cost in rows if count > 0 and regex.search(name)]
    if not matches:
        return None
    return CtestPrediction(sum_cost_s=sum(matches), n_tests=len(matches))
