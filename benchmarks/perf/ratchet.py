"""Work-count perf ratchet: exact counts of work per workload, never milliseconds.

For every workload in ``workloads.py`` this counts, without editing the code
under test:

- ``py_calls``: Python function calls whose code lives under ``src/jobd`` or
  ``src/job_cli`` (``sys.setprofile`` + ``threading.setprofile``, so the
  threads Starlette's TestClient runs requests on are counted too), keyed
  ``relpath:qualname``. Comprehension frames are skipped: Python 3.11 runs
  them as functions and 3.12 inlines them (PEP 709), so counting them would
  make the totals depend on the interpreter.
- ``matcher_calls``: the subset of ``py_calls`` in ``src/jobd/matcher.py``,
  i.e. matcher evaluations.
- ``sql``: statements the broker sends to SQLite (SQLAlchemy
  ``before_cursor_execute`` on the app's own engine), keyed by verb + table.
- ``http``: requests that reach the in-process broker (an httpx request hook
  on the TestClient), keyed by method + path with ids folded to ``{id}``. For
  the ``mcp_*`` workloads this is the MCP-tool → broker request count.

Each workload runs twice on two fresh, identical fixtures; only the second run
is counted, so one-time costs (first import, schema build, a lazy cache) are
not charged to whichever workload happens to run first.

The gate: every count must EQUAL ``baseline.json``. A rise fails and prints
the per-function deltas; raising the baseline needs ``--update
--allow-increase``. A drop also fails, until ``--update`` records it, so a
gain is locked in by the PR that earned it.

    uv run python benchmarks/perf/ratchet.py            # check (exit 1 on any difference)
    uv run python benchmarks/perf/ratchet.py --update   # lower the baseline to what was measured
    uv run python benchmarks/perf/ratchet.py --update --allow-increase   # accept a rise
"""

from __future__ import annotations

import argparse
import dis
import inspect
import json
import os
import re
import socket
import sys
import tempfile
import threading
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SRC = ROOT / "src"
BASELINE = HERE / "baseline.json"
# Counted roots: the package's own source, nothing from site-packages or stdlib.
ROOTS = tuple(str(SRC / p) + os.sep for p in ("jobd", "job_cli"))
_INLINED = ("<listcomp>", "<dictcomp>", "<setcomp>")
METRICS = ("py_calls", "matcher_calls", "sql", "http")


class _NoNetwork(RuntimeError):
    pass


def _tripwire(self: socket.socket, address: Any) -> None:
    raise _NoNetwork(f"perf workloads must not open sockets (tried {address!r})")


class Counts:
    """One workload's tallies. Only one is active at a time."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.sql: Counter[str] = Counter()
        self.http: Counter[str] = Counter()


_active: Counts | None = None
# code object -> (key, entry offset) or None when the code is not counted.
_keys: dict[Any, tuple[str, int | None] | None] = {}
_ASYNC = inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR | inspect.CO_ITERABLE_COROUTINE


def _entry_offset(code: Any) -> int | None:
    """For a coroutine, the offset of its ``RESUME 0`` (function start).

    The profiler reports a "call" on every resume after an ``await`` too, and
    how often a coroutine suspends is the event loop's business: on the same
    workload ``next_job`` resumed 2 times fewer on 3.13 than on 3.11. So a
    coroutine counts once per entry. Plain generators keep counting each
    resume (one per item), which is the same on every version measured."""
    if not code.co_flags & _ASYNC:
        return None
    for ins in dis.get_instructions(code):
        if ins.opname == "RESUME" and ins.arg == 0:
            return ins.offset
    raise RuntimeError(f"no RESUME 0 in {code.co_qualname}")


def _key(code: Any) -> tuple[str, int | None] | None:
    k = _keys.get(code, ())
    if k != ():
        return k  # type: ignore[return-value]
    fn = code.co_filename
    name = code.co_qualname
    if fn.startswith(ROOTS) and not name.endswith(_INLINED):
        k = (f"{Path(fn).relative_to(SRC).as_posix()}:{name}", _entry_offset(code))
    else:
        k = None
    _keys[code] = k
    return k


def _profile(frame: Any, event: str, arg: Any) -> None:
    if event != "call":
        return
    c = _active
    if c is None:
        return
    k = _key(frame.f_code)
    if k is not None and (k[1] is None or frame.f_lasti == k[1]):
        c.calls[k[0]] += 1


_TABLE = re.compile(r"\b(?:FROM|INTO|UPDATE|JOIN)\s+\"?(\w+)", re.IGNORECASE)
_ID = re.compile(r"/\d+(?=/|$)")


def _on_sql(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
    c = _active
    if c is None:
        return
    verb = statement.split(None, 1)[0].upper() if statement.strip() else "?"
    m = _TABLE.search(statement)
    c.sql[f"{verb} {m.group(1) if m else '-'}"] += 1


def _on_request(request: Any) -> None:
    c = _active
    if c is None:
        return
    c.http[f"{request.method} {_ID.sub('/{id}', request.url.path)}"] += 1


def _instrument(fx: Any) -> None:
    from sqlalchemy import event

    event.listen(fx.app.state.engine, "before_cursor_execute", _on_sql)
    fx.client.event_hooks["request"].append(_on_request)


def run_workload(name: str, template: Path, tmp: Path) -> dict[str, Any]:
    global _active
    from workloads import WORKLOADS, open_fixture

    fn = WORKLOADS[name]
    fn(open_fixture(template, tmp / f"{name}-warm"))  # one-time costs land here, uncounted
    fx = open_fixture(template, tmp / f"{name}-run")
    _instrument(fx)
    counts = Counts()
    threading.setprofile(_profile)
    sys.setprofile(_profile)
    _active = counts
    try:
        witness = fn(fx)
    finally:
        _active = None
        sys.setprofile(None)
        threading.setprofile(None)  # type: ignore[arg-type]
    matcher = sum(v for k, v in counts.calls.items() if k.startswith("jobd/matcher.py:"))
    return {
        # Through JSON, so a tuple compares equal to the list the baseline holds.
        "witness": json.loads(json.dumps(witness)),
        "py_calls": sum(counts.calls.values()),
        "matcher_calls": matcher,
        "sql": sum(counts.sql.values()),
        "http": sum(counts.http.values()),
        "detail": {
            "py_calls": dict(sorted(counts.calls.items())),
            "sql": dict(sorted(counts.sql.items())),
            "http": dict(sorted(counts.http.items())),
        },
    }


def measure(names: list[str] | None = None) -> dict[str, Any]:
    sys.path.insert(0, str(HERE))
    from workloads import WORKLOADS, build_template

    import jobd

    # Counts are keyed on files under src/. A non-editable install would run
    # jobd from site-packages and every count would read 0, which is a pass
    # for nothing. Refuse instead.
    if not Path(jobd.__file__).resolve().is_relative_to(SRC):
        raise SystemExit(f"jobd imported from {jobd.__file__}, not {SRC}: use the repo's venv")

    socket.socket.connect = _tripwire  # type: ignore[method-assign]
    os.environ["JOBD_ALLOW_NO_AUTH"] = "1"
    os.environ["JOBD_DISABLE_TAILNET_ACL"] = "1"
    for var in ("JOBD_URL", "JOBD_API_TOKEN"):
        os.environ.pop(var, None)
    out: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="jobd-perf-") as d:
        template = Path(d) / "template"
        build_template(template)
        for name in names or list(WORKLOADS):
            out[name] = run_workload(name, template, Path(d))
    return out


def compare(base: dict[str, Any], got: dict[str, Any]) -> list[str]:
    """Every difference between a baseline and a measurement, as report lines.
    Empty means the gate passes."""
    problems: list[str] = []
    for name in sorted(set(base) | set(got)):
        if name not in got:
            problems.append(f"{name}: in the baseline but not measured")
            continue
        if name not in base:
            problems.append(f"{name}: measured but not in the baseline (run --update)")
            continue
        b, g = base[name], got[name]
        if b["witness"] != g["witness"]:
            problems.append(f"{name}: witness changed {b['witness']!r} -> {g['witness']!r}")
        for m in METRICS:
            if b[m] != g[m]:
                problems.append(f"{name}: {m} {b[m]} -> {g[m]} ({g[m] - b[m]:+d})")
                kind = "py_calls" if m == "matcher_calls" else m
                problems.extend(_top_deltas(b["detail"][kind], g["detail"][kind]))
    return problems


def _top_deltas(b: dict[str, int], g: dict[str, int], n: int = 10) -> list[str]:
    deltas = {k: g.get(k, 0) - b.get(k, 0) for k in set(b) | set(g)}
    moved = sorted((k for k, v in deltas.items() if v), key=lambda k: (-abs(deltas[k]), k))
    return [f"    {deltas[k]:+d}  {k}" for k in moved[:n]]


def update(base: dict[str, Any], got: dict[str, Any], allow_increase: bool) -> list[str]:
    """Refusals for an --update; empty means it may be written."""
    refusals = []
    for name, g in got.items():
        b = base.get(name)
        if b is None:
            continue
        for m in METRICS:
            if g[m] > b[m] and not allow_increase:
                refusals.append(f"{name}: {m} would rise {b[m]} -> {g[m]}; needs --allow-increase")
    return refusals


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--update", action="store_true", help="write the measurement as the baseline")
    ap.add_argument("--allow-increase", action="store_true", help="let --update raise a count")
    ap.add_argument("--only", default="", help="comma-separated workload names")
    ap.add_argument("--baseline", default=str(BASELINE))
    ap.add_argument("--json-out", default="", help="also write the measurement here")
    a = ap.parse_args(argv)
    names = [n for n in a.only.split(",") if n] or None
    got = measure(names)
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    path = Path(a.baseline)
    base = json.loads(path.read_text())["workloads"] if path.exists() else {}
    if names:
        base = {k: v for k, v in base.items() if k in names}
    for name in sorted(got):
        g = got[name]
        print(f"{name:32} py_calls={g['py_calls']:6} matcher={g['matcher_calls']:4} "
              f"sql={g['sql']:4} http={g['http']:3}")  # fmt: skip
    if a.update:
        refusals = update(base, got, a.allow_increase)
        if refusals:
            print("\n".join(refusals), file=sys.stderr)
            return 1
        full = json.loads(path.read_text())["workloads"] if path.exists() else {}
        full.update(got)
        path.write_text(
            json.dumps(
                {"python_roots": ["src/jobd", "src/job_cli"], "workloads": full},
                indent=1,
                sort_keys=True,
            )
            + "\n"
        )
        print(f"wrote {path.name}")
        return 0
    problems = compare(base, got)
    if problems:
        print("perf ratchet: counts differ from the baseline", file=sys.stderr)
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(f"perf ratchet: {len(got)} workloads equal the baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
