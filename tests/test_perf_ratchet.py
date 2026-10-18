"""The work-count perf ratchet (benchmarks/perf): exact counts, zero headroom.

The gate runs ``ratchet.py`` in a fresh interpreter, so what earlier tests
imported, cached or monkeypatched cannot move the counts, and the coverage
tracer pytest-cov would start in a subprocess is kept out of it. The other
tests pin the comparison rules on hand-made tables, each beside the case that
must pass, so a comparer that always passed or always failed fails here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks" / "perf"))

from ratchet import compare, update  # noqa: E402


def _row(py: dict[str, int], sql: int = 2, http: int = 1, witness: object = None) -> dict:
    return {
        "witness": witness if witness is not None else {"ids": [1, 2]},
        "py_calls": sum(py.values()),
        "matcher_calls": sum(v for k, v in py.items() if k.startswith("jobd/matcher.py:")),
        "sql": sql,
        "http": http,
        "detail": {"py_calls": py, "sql": {"SELECT jobs": sql}, "http": {"GET /jobs": http}},
    }


BASE = {
    "sched_pass": _row({"jobd/matcher.py:fits_on_worker": 10, "jobd/db.py:load": 3}),
    "api_workers": _row({"jobd/broker/routes/workers.py:list_workers": 1}),
}


@pytest.mark.timeout(900)
def test_the_counted_workloads_equal_the_committed_baseline() -> None:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("COV_CORE_", "COVERAGE_")) and k not in ("JOBD_URL", "JOBD_API_TOKEN")
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    r = subprocess.run(
        [sys.executable, str(ROOT / "benchmarks" / "perf" / "ratchet.py")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=850,
    )
    assert r.returncode == 0, f"perf ratchet failed:\n{r.stdout}\n{r.stderr}"
    assert "18 workloads equal the baseline" in r.stdout, r.stdout


def test_an_extra_call_names_the_function_and_its_delta_and_spares_other_rows() -> None:
    assert compare(BASE, BASE) == []
    got = {
        "sched_pass": _row(
            {
                "jobd/matcher.py:fits_on_worker": 10,
                "jobd/db.py:load": 3,
                "jobd/matcher.py:_effective_vram_gb": 4,
            }
        ),  # fmt: skip
        "api_workers": BASE["api_workers"],
    }
    lines = compare(BASE, got)
    assert "sched_pass: py_calls 13 -> 17 (+4)" in lines
    assert "sched_pass: matcher_calls 10 -> 14 (+4)" in lines
    assert "    +4  jobd/matcher.py:_effective_vram_gb" in lines
    assert not any(ln.startswith("api_workers") for ln in lines)


def test_a_drop_also_fails_until_it_is_recorded() -> None:
    got = {**BASE, "api_workers": _row({}, sql=1)}
    lines = compare(BASE, got)
    assert "api_workers: sql 2 -> 1 (-1)" in lines
    assert "api_workers: py_calls 1 -> 0 (-1)" in lines
    assert update(BASE, got, allow_increase=False) == []  # a drop may be written


def test_raising_the_baseline_needs_allow_increase() -> None:
    got = {**BASE, "api_workers": _row({"jobd/broker/routes/workers.py:list_workers": 1}, http=2)}
    assert update(BASE, got, allow_increase=False) == [
        "api_workers: http would rise 1 -> 2; needs --allow-increase"
    ]
    assert update(BASE, got, allow_increase=True) == []


def test_a_changed_witness_fails_even_when_every_count_is_equal() -> None:
    """A workload that silently ran something else (a 404 path, a default
    branch) can cost the same; only its output tells."""
    got = {**BASE, "api_workers": _row({"jobd/broker/routes/workers.py:list_workers": 1},
                                        witness={"ids": []})}  # fmt: skip
    assert compare(BASE, got) == ["api_workers: witness changed {'ids': [1, 2]} -> {'ids': []}"]


def test_update_refuses_a_changed_witness_without_its_flag() -> None:
    same = {**BASE}
    assert update(BASE, same, allow_increase=False) == []  # positive control
    got = {**BASE, "api_workers": {**BASE["api_workers"], "witness": {"ids": []}}}
    assert update(BASE, got, allow_increase=True) == [
        "api_workers: witness changed; needs --allow-witness-change"
    ]
    assert update(BASE, got, allow_increase=False, allow_witness_change=True) == []


def test_update_writes_nothing_when_it_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the CLI: a refused --update exits 1 and leaves the
    baseline file byte-identical; the flagged one writes it."""
    import json

    import ratchet

    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"python_roots": [], "workloads": BASE}))
    before = path.read_bytes()
    risen = {**BASE, "api_workers": _row({"jobd/broker/routes/workers.py:list_workers": 1}, http=2)}
    monkeypatch.setattr(ratchet, "measure", lambda names=None: risen)
    assert ratchet.main(["--update", "--baseline", str(path)]) == 1
    assert path.read_bytes() == before
    assert ratchet.main(["--update", "--allow-increase", "--baseline", str(path)]) == 0
    assert json.loads(path.read_text())["workloads"]["api_workers"]["http"] == 2
    changed = {**risen, "sched_pass": {**risen["sched_pass"], "witness": {"ids": [9]}}}
    monkeypatch.setattr(ratchet, "measure", lambda names=None: changed)
    after_rise = path.read_bytes()
    assert ratchet.main(["--update", "--allow-increase", "--baseline", str(path)]) == 1
    assert path.read_bytes() == after_rise
    assert ratchet.main(["--update", "--allow-witness-change", "--baseline", str(path)]) == 0
    assert json.loads(path.read_text())["workloads"]["sched_pass"]["witness"] == {"ids": [9]}


def test_a_workload_missing_from_either_side_fails() -> None:
    assert compare(BASE, {"sched_pass": BASE["sched_pass"]}) == [
        "api_workers: in the baseline but not measured"
    ]
    assert compare({"sched_pass": BASE["sched_pass"]}, BASE) == [
        "api_workers: measured but not in the baseline (run --update)"
    ]
