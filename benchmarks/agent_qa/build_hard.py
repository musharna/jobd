"""Draw the harder question set (version 2) from a broker and freeze its answers.

The 10-16 set put both tool setups at 41 of 41. These questions come from the
uses jobd's README and agent cookbook document: what ran or failed in a time
window, aggregation over many jobs (busiest host or project, total run time),
the event stream as a post-mortem record (dispatch skips, queue wait), and a
join of a window's failures with their logs. None asks for something only one
setup can see: every field used is shown by `job status` / `job logs` /
`job audit` and by jobd-mcp's read tools. (Dependency chains were left out:
`job status` does not print depends_on.)

Like ``build_questions.py``, this reads the broker's HTTP API over httpx and
never imports ``jobd``, so a bug in jobd-mcp or its client cannot also be in
the key. It sends only GETs. Each kind's answer is computed by one function
(``TRUTH``), and ``drift`` recomputes every answer with the same function
before a sweep, so a pruned job, a rotated event file or a resurrected job
stops the sweep instead of moving the key under it.

Fairness rules, applied when drawing:

- every window is closed and ended more than two hours before the freeze, and
  every job counted reached a terminal state more than an hour before it;
- no job that a window question counts finished within two minutes of either
  edge of the window, so a rounding or inclusive/exclusive slip cannot decide
  it (the question still states: start inclusive, end exclusive, UTC);
- "most" questions have a unique top answer;
- projects whose names fold together (Foo / foo, a-b / a_b) are left out, as
  in version 1;
- event questions use jobs whose whole life is inside the event file's
  retained span; log questions use jobs from the last 21 days.

    JOBD_URL=... JOBD_API_TOKEN=... uv run python benchmarks/agent_qa/build_hard.py
"""

from __future__ import annotations

import json
import os
import random
import signal
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_questions import (
    ARTIFACTS,
    PAGE,
    POOL_ALIASES,
    Broker,
    fleet_snapshot,
    host_names,
    last_line,
    ts,
)

SEED = 20261018
VERSION = 2
# A separate directory: the 10-16 key and results stay as they are.
OUT = Path(os.environ.get("JOBD_QA_HARD") or ARTIFACTS / "rerun-2026-10-18")
ALL_TERMINAL = ("completed", "failed", "cancelled", "preempted", "orphaned", "scheduling_timeout")
EDGE = timedelta(minutes=2)
SETTLED = timedelta(hours=1)
WINDOW_GAP = timedelta(hours=2)
EARLIEST = datetime(2026, 9, 15, tzinfo=UTC)
LOG_WINDOW = timedelta(days=21)


def all_jobs(b: Broker) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page, total = b.get_with_total("/jobs", limit=PAGE, offset=offset)
        rows.extend(page)
        offset += PAGE
        if offset >= total:
            break
    by_id = {r["id"]: r for r in rows}  # a mid-scan submit repeats a row
    return sorted(by_id.values(), key=lambda r: r["id"])


def fin(j: dict[str, Any]) -> datetime | None:
    return ts(j["finished_at"]) if j.get("finished_at") else None


def in_window(j: dict[str, Any], a: datetime, z: datetime) -> bool:
    f = fin(j)
    return f is not None and a <= f < z


def near_edge(jobs: list[dict[str, Any]], a: datetime, z: datetime) -> bool:
    return any((f := fin(j)) is not None and (abs(f - a) < EDGE or abs(f - z) < EDGE) for j in jobs)


def iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def window_text(a: datetime, z: datetime) -> str:
    return (
        f"a finish time (finished_at) at or after {iso(a)} and before {iso(z)} (UTC; start "
        "inclusive, end exclusive)"
    )


def duration(j: dict[str, Any]) -> float:
    return (ts(j["finished_at"]) - ts(j["started_at"])).total_seconds()


# --- answers, one function per kind; build and drift both call these ---------


def _window(p: dict[str, Any]) -> tuple[datetime, datetime]:
    return ts(p["after"]), ts(p["before"])


def t_window_count(p, jobs, b):
    a, z = _window(p)
    n = sum(j["project"] == p["project"] and j["state"] == p["state"] and in_window(j, a, z)
            for j in jobs)  # fmt: skip
    return {"count": n}


def t_window_ids(p, jobs, b):
    a, z = _window(p)
    return {
        "job_ids": sorted(j["id"] for j in jobs if j["state"] == "failed" and in_window(j, a, z))
    }


def t_busiest_host(p, jobs, b):
    a, z = _window(p)
    c = Counter(
        j["worker"]
        for j in jobs
        if j["project"] == p["project"] and j["worker"] and in_window(j, a, z)
    )
    return {"tally": dict(sorted(c.items()))}


def t_busiest_project(p, jobs, b):
    a, z = _window(p)
    c = Counter(j["project"] for j in jobs if j["state"] == p["state"] and in_window(j, a, z))
    return {"tally": dict(sorted(c.items()))}


def t_total_runtime(p, jobs, b):
    a, z = _window(p)
    js = [j for j in jobs if j["project"] == p["project"] and j["state"] == "completed"
          and in_window(j, a, z)]  # fmt: skip
    return {
        "seconds": round(sum(duration(j) for j in js), 3),
        "job_ids": sorted(j["id"] for j in js),
    }


def job_events(b: Broker, job_id: int) -> list[dict[str, Any]]:
    return b.get("/events", job_id=job_id, since="60d", limit=10000)


def t_event_count(p, jobs, b):
    ev = job_events(b, p["job_id"])
    return {"count": sum(e["event"] == p["event"] for e in ev)}


def t_queue_wait(p, jobs, b):
    ev = [e for e in job_events(b, p["job_id"]) if e["event"] == "job_dispatched"]
    return {"dispatches": len(ev), "seconds": ev[0]["payload"]["queue_wait_s"] if ev else None}


def t_log_join(p, jobs, b):
    a, z = _window(p)
    cands = [j for j in jobs if j["project"] == p["project"] and j["state"] == "failed"
             and in_window(j, a, z)]  # fmt: skip
    lines = {}
    for j in cands:
        out = b.get(f"/jobs/{j['id']}/output", tail=4096)
        lines[str(j["id"])] = None if out.get("pruned") else last_line(out.get("tail") or "")
    return {"lines": lines}


TRUTH = {
    "window_count": t_window_count,
    "window_ids": t_window_ids,
    "busiest_host": t_busiest_host,
    "busiest_project": t_busiest_project,
    "total_runtime": t_total_runtime,
    "event_count": t_event_count,
    "queue_wait": t_queue_wait,
    "log_join": t_log_join,
}


def unique_top(tally: dict[str, int]) -> str | None:
    top = sorted(tally.items(), key=lambda kv: -kv[1])
    if len(top) >= 2 and top[0][1] > top[1][1]:
        return top[0][0]
    return None


# --- drawing -----------------------------------------------------------------


def random_window(rng: random.Random, hours: list[int], now: datetime) -> tuple[datetime, datetime]:
    span = hours[rng.randrange(len(hours))]
    latest = now - WINDOW_GAP - timedelta(hours=span)
    n_hours = int((latest - EARLIEST).total_seconds() // 3600)
    a = EARLIEST + timedelta(hours=rng.randrange(n_hours))
    return a, a + timedelta(hours=span)


def _q(cat: str, question: str, fmt: str, params: dict, truth: dict, source: str) -> dict:
    return {"category": cat, "question": question, "answer_format": fmt, "params": params,
            "truth": truth, "source": source}  # fmt: skip


def build(b: Broker, rng: random.Random, now: datetime) -> dict[str, Any]:
    workers = b.get("/workers")
    jobs = all_jobs(b)
    # Terminal more than an hour ago. A job still running has no finished_at,
    # and when it finishes it lands after every window (each ends 2 h before
    # the freeze), so no window's answer can move later.
    settled = [
        j for j in jobs if (f := fin(j)) and f < now - SETTLED and j["state"] in ALL_TERMINAL
    ]
    folded: dict[str, set[str]] = {}
    for j in jobs:
        folded.setdefault(j["project"].casefold().replace("-", "_"), set()).add(j["project"])
    clean = sorted(p for names in folded.values() if len(names) == 1 for p in names)
    qs: list[dict[str, Any]] = []

    def windowed(project: str | None) -> list[dict[str, Any]]:
        return [j for j in settled if project is None or j["project"] == project]

    def tries(n: int, make) -> None:
        got = 0
        for _ in range(20000):
            q = make()
            if q is not None:
                qs.append(q)
                got += 1
                if got == n:
                    return
        raise RuntimeError(f"could not draw {n} questions for {make.__name__}")

    busy = [p for p in clean if sum(j["project"] == p for j in settled) >= 20]
    used_proj: Counter[str] = Counter()

    def window_count():
        p, s = rng.choice(busy), rng.choice(["completed", "failed", "cancelled"])
        a, z = random_window(rng, [6, 12, 24, 48, 72], now)
        params = {"project": p, "state": s, "after": iso(a), "before": iso(z)}
        t = t_window_count(params, jobs, b)
        if not 4 <= t["count"] <= 30 or used_proj[p] or near_edge(windowed(p), a, z):
            return None
        used_proj[p] += 1
        return _q("window_count",
                  f"How many jobd jobs of project '{p}' ended in state '{s}' with "
                  f"{window_text(a, z)}?",
                  "an integer, e.g. 12", params, t,
                  "GET /jobs (every page), project and state equal, finished_at in the window")  # fmt: skip

    def window_ids():
        a, z = random_window(rng, [2, 3, 4, 6, 8, 12], now)
        params = {"after": iso(a), "before": iso(z)}
        t = t_window_ids(params, jobs, b)
        projects = {j["project"] for j in jobs if j["id"] in set(t["job_ids"])}
        if not 2 <= len(t["job_ids"]) <= 8 or len(projects) < 2 or near_edge(settled, a, z):
            return None
        for q in qs:
            if q["category"] == "window_ids" and (
                ts(q["params"]["after"]) < z and a < ts(q["params"]["before"])
            ):
                return None  # windows overlap: the answers would share jobs
        return _q("window_ids",
                  f"Which jobd jobs, of any project, ended in state 'failed' with "
                  f"{window_text(a, z)}? List every one.",
                  "a comma-separated list of job ids, e.g. 4012, 4013", params, t,
                  "GET /jobs (every page), state failed, finished_at in the window")  # fmt: skip

    used_host_proj: set[str] = set()

    def busiest_host():
        p = rng.choice(busy)
        a, z = random_window(rng, [12, 24, 48, 72, 168], now)
        params = {"project": p, "after": iso(a), "before": iso(z)}
        t = t_busiest_host(params, jobs, b)
        n = sum(t["tally"].values())
        top = unique_top(t["tally"])
        if top is None or not 5 <= n <= 60 or p in used_host_proj or near_edge(windowed(p), a, z):
            return None
        if sum(q["category"] == "busiest_host" and q["truth"]["names"][0] == top for q in qs) >= 2:
            return None  # spread the answers over the hosts
        used_host_proj.add(p)
        t["names"] = host_names(top, workers)
        return _q("busiest_host",
                  f"Among the jobd jobs of project '{p}' that ran on a worker and have "
                  f"{window_text(a, z)}, in any end state, which worker host ran the most?",
                  "a hostname, e.g. alpha", params, t,
                  "GET /jobs (every page): per-worker count of the project's jobs in the window")  # fmt: skip

    used_bp: set[str] = set()

    def busiest_project():
        s = ["completed", "failed"][len(used_bp) % 2]
        a, z = random_window(rng, [6, 12, 24, 48], now)
        params = {"state": s, "after": iso(a), "before": iso(z)}
        t = t_busiest_project(params, jobs, b)
        top = unique_top(t["tally"])
        if top is None or top not in clean or len(t["tally"]) < 3 or near_edge(settled, a, z):
            return None
        if params["after"] in used_bp or any(
            q["category"] == "busiest_project" and q["truth"]["project"] == top for q in qs
        ):
            return None  # one question per top project, so no constant gets two
        used_bp.add(params["after"])
        t["project"] = top
        return _q("busiest_project",
                  f"Which jobd project had the most jobs that ended in state '{s}' with "
                  f"{window_text(a, z)}?",
                  "a project name, e.g. myproj", params, t,
                  "GET /jobs (every page): per-project count of jobs in that state in the window")  # fmt: skip

    used_rt: set[str] = set()

    def total_runtime():
        p = rng.choice(busy)
        a, z = random_window(rng, [6, 12, 24, 48], now)
        params = {"project": p, "after": iso(a), "before": iso(z)}
        t = t_total_runtime(params, jobs, b)
        if not 3 <= len(t["job_ids"]) <= 10 or p in used_rt or near_edge(windowed(p), a, z):
            return None
        used_rt.add(p)
        # Each job's run time is shown to the microsecond; summing whole
        # seconds moves the total by under 1 s per job.
        t["tolerance_s"] = float(len(t["job_ids"]))
        return _q("total_runtime",
                  f"What is the total run time, summed over every jobd job of project '{p}' "
                  f"that ended in state 'completed' with {window_text(a, z)}? A job's run time "
                  "is its finish time (finished_at) minus its start time (started_at).",
                  "a number of seconds, rounded to the nearest second, e.g. 754", params, t,
                  "GET /jobs (every page): sum of finished_at - started_at")  # fmt: skip

    # Event questions: jobs whose whole life is in the retained event span.
    oldest_event = ts(b.get("/events", since="60d", limit=10000)[0]["ts"])
    recent = [j for j in settled if ts(j["submitted_at"]) > oldest_event + timedelta(hours=6)]
    rng.shuffle(recent)
    seen_ids: set[int] = set()
    skip_q: list[dict[str, Any]] = []
    wait_q: list[dict[str, Any]] = []
    for j in recent:
        if len(skip_q) == 3 and len(wait_q) == 3:
            break
        if j["id"] in seen_ids or not j["started_at"]:
            continue
        ev = job_events(b, j["id"])
        if not any(e["event"] == "job_submitted" for e in ev):
            continue  # its start is not in the file
        warns = sum(e["event"] == "sweep_warning" for e in ev)
        disp = [e for e in ev if e["event"] == "job_dispatched"]
        # sweep_warning, not dispatch_skip: almost every job has exactly 3
        # skips (one per host its pin rules out), so a constant would pass.
        if len(skip_q) < 3 and 5 <= warns <= 40:
            params = {"job_id": j["id"], "event": "sweep_warning"}
            skip_q.append(_q("event_count",
                f"While job {j['id']} waited, the jobd broker's sweeper recorded warnings about "
                "it (events of type 'sweep_warning', e.g. why it was blocked). How many "
                "sweep_warning events are there for that job?",
                "an integer, e.g. 12", params, t_event_count(params, jobs, b),
                f"GET /events?job_id={j['id']}: count of sweep_warning"))  # fmt: skip
            seen_ids.add(j["id"])
        elif len(wait_q) < 3 and len(disp) == 1 and disp[0]["payload"].get("queue_wait_s", 0) >= 30:
            params = {"job_id": j["id"]}
            wait_q.append(_q("queue_wait",
                f"According to the jobd broker's event record, how long did job {j['id']} wait "
                "in the queue before it was dispatched to a worker (the queue_wait_s of its "
                "job_dispatched event)?",
                "a number of seconds, rounded to the nearest second, e.g. 754", params,
                t_queue_wait(params, jobs, b),
                f"GET /events?job_id={j['id']}: job_dispatched payload queue_wait_s"))  # fmt: skip
            seen_ids.add(j["id"])
    if len(skip_q) < 3 or len(wait_q) < 3:
        raise RuntimeError(f"event questions: {len(skip_q)} skip, {len(wait_q)} wait")

    used_lj: set[str] = set()

    def log_join():
        p = rng.choice(busy)
        a, z = random_window(rng, [12, 24, 48], now)
        if a < now - LOG_WINDOW:
            return None
        params = {"project": p, "after": iso(a), "before": iso(z)}
        cands = [j for j in windowed(p) if j["state"] == "failed" and in_window(j, a, z)]
        if not 2 <= len(cands) <= 5 or p in used_lj or near_edge(windowed(p), a, z):
            return None
        t = t_log_join(params, jobs, b)
        lines = t["lines"]
        if any(v is None for v in lines.values()) or len(set(lines.values())) != len(lines):
            return None
        target = rng.choice(sorted(lines))
        used_lj.add(p)
        params["line"] = lines[target]
        t["job_id"] = int(target)
        return _q("log_join",
                  f"Among the jobd jobs of project '{p}' that ended in state 'failed' with "
                  f"{window_text(a, z)}, one job's captured output (its log) has this as its "
                  f"last non-empty line:\n\n    {lines[target]}\n\nWhich job is it?",
                  "a job id, e.g. 4012", params, t,
                  "GET /jobs (every page) for the window's failures, then GET /jobs/{id}/output")  # fmt: skip

    tries(6, window_count)
    tries(6, window_ids)
    tries(5, busiest_host)
    tries(5, busiest_project)
    tries(5, total_runtime)
    qs.extend(skip_q + wait_q)
    tries(5, log_join)

    known = {a.lower(): w["host"] for w in workers for a in host_names(w["host"], workers)}
    known.update({j["worker"].lower(): j["worker"] for j in jobs if j["worker"]})
    known = {k: v for k, v in known.items() if k not in POOL_ALIASES}
    for q in qs:
        if q["category"] == "busiest_host":
            q["truth"]["known_names"] = dict(sorted(known.items()))
    for i, q in enumerate(qs, 1):
        q["id"] = f"h{i:02d}"
    return {
        "version": VERSION,
        "built": now.isoformat(timespec="seconds"),
        "seed": SEED,
        "broker_version": b.get("/health").get("version"),
        "fleet": fleet_snapshot(workers),
        "questions": qs,
    }


def drift(b: Broker, key: dict[str, Any]) -> list[str]:
    """Recompute every answer with the function that built it; empty = safe."""
    jobs = all_jobs(b)
    problems = []
    for q in key["questions"]:
        got = TRUTH[q["category"]](q["params"], jobs, b)
        want = {k: v for k, v in q["truth"].items() if k in got}
        if got != want:
            problems.append(f"{q['id']} ({q['category']}): answer changed")
    return problems


def main() -> None:
    signal.signal(signal.SIGALRM, lambda *_: sys.exit("aborting: walltime guard"))
    signal.alarm(1200)
    url, token = os.environ.get("JOBD_URL"), os.environ.get("JOBD_API_TOKEN", "")
    if not url:
        sys.exit("set JOBD_URL (and JOBD_API_TOKEN) to the broker to draw from")
    b = Broker(url, token)
    out = OUT / "questions.json"
    if out.exists():
        sys.exit(f"{out} exists: the key is frozen; move it away to rebuild")
    key = build(b, random.Random(SEED), datetime.now(UTC))
    OUT.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(key, indent=1) + "\n")
    print(len(key["questions"]), dict(Counter(q["category"] for q in key["questions"])),
          "frozen", key["built"], "->", out)  # fmt: skip


if __name__ == "__main__":
    main()
