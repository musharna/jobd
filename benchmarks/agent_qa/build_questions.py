"""Draw the agent benchmark's questions from a broker and freeze their answers.

The answers come straight from the broker's HTTP API over httpx. This script
never imports ``jobd``, so a bug in jobd-mcp or in jobd's client cannot also
be in the key. It sends only GETs, plus ``POST /submit`` with
``"dry_run": true`` to cross-check the routing answers (``_dry_run`` refuses
to send anything else).

Every job question is about a job already in a terminal state (completed or
failed) that finished more than an hour before the freeze, so its fields can
no longer change. Jobs are drawn at random (seeded), not picked. The fleet
questions are about worker configuration (tags, slots), which does change
when someone reconfigures a host; ``run.py`` re-reads it before a sweep and
refuses to run if it drifted (``drift`` below).

The broker in question is the user's own, so ``questions.json`` holds their
project names, job ids and log lines; it is gitignored, not published.

    JOBD_URL=... JOBD_API_TOKEN=... uv run python benchmarks/agent_qa/build_questions.py
"""

from __future__ import annotations

import json
import os
import random
import re
import signal
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

SEED = 20261016
HERE = Path(__file__).resolve().parent
TERMINAL = ("completed", "failed")
POOL_ALIASES = {"any", "any-gpu"}  # shared by every worker; they name no host
PAGE = 1000  # the broker's LIST_LIMIT_MAX
SETTLED = timedelta(hours=1)
LOG_WINDOW = timedelta(days=21)  # the broker prunes logs after ~60 days
FAIL_WINDOW = timedelta(days=45)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class Broker:
    def __init__(self, url: str, token: str) -> None:
        self.http = httpx.Client(
            base_url=url.rstrip("/"),
            timeout=60,
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )

    def get(self, path: str, **params: Any) -> Any:
        r = self.http.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def get_with_total(self, path: str, **params: Any) -> tuple[Any, int]:
        r = self.http.get(path, params=params)
        r.raise_for_status()
        return r.json(), int(r.headers["X-Total-Count"])

    def _dry_run(self, body: dict[str, Any]) -> dict[str, Any]:
        if body.get("dry_run") is not True:
            raise ValueError("refusing to POST /submit without dry_run=true")
        r = self.http.post("/submit", json=body)
        r.raise_for_status()
        plan = r.json()
        if plan.get("state") != "dry-run":
            raise RuntimeError(f"broker did not treat the submit as a dry run: {plan!r}")
        return plan

    def would_route_to(self, needs: list[str]) -> list[str]:
        plan = self._dry_run(
            {
                "cmd": ["true"],
                "cwd": "/tmp",
                "project": "agent-qa-dry-run",
                "host_pin": "any",
                "requires": {"needs": needs},
                "dry_run": True,
            }
        )
        return sorted(plan["would_route_to"])

    def terminal_jobs(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for state in TERMINAL:
            offset = 0
            while True:
                page, total = self.get_with_total(
                    "/jobs", state_filter=state, limit=PAGE, offset=offset
                )
                rows.extend(page)
                offset += PAGE
                if offset >= total:
                    break
        return rows


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def duration_s(job: dict[str, Any]) -> float:
    return (ts(job["finished_at"]) - ts(job["started_at"])).total_seconds()


def host_names(host: str, workers: list[dict[str, Any]]) -> list[str]:
    """A host and every alias that names only it."""
    for w in workers:
        if w["host"] == host:
            return [host, *[a for a in w.get("host_aliases") or [] if a not in POOL_ALIASES]]
    return [host]


def last_line(tail: str) -> str | None:
    """The last non-empty line of a log, or None if it is not a fair question
    (control characters, carriage-return progress bars, ANSI colour, too short
    or too long to quote)."""
    lines = [ln for ln in tail.split("\n") if ln.strip()]
    if not lines:
        return None
    line = lines[-1].rstrip()
    if "\r" in line or _ANSI.search(line) or "ANSWER" in line.upper():
        return None
    if not line.isprintable() or not 8 <= len(line.strip()) <= 160:
        return None
    return line.strip()


def online(workers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [w for w in workers if w["state"] == "online"]


def eligible(needs: list[str], workers: list[dict[str, Any]]) -> list[str]:
    """Online workers whose tags include every needed tag: the documented
    routing rule for a job with tags and no other requirement."""
    return sorted(w["host"] for w in online(workers) if set(needs) <= set(w["tags"]))


def gpu_tier_gb(w: dict[str, Any]) -> int:
    tiers = [int(m.group(1)) for t in w["tags"] if (m := re.fullmatch(r"cuda-(\d+)gb", t))]
    return max(tiers, default=0)


def fleet_snapshot(workers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = ("host", "host_aliases", "state", "tags", "gpu", "max_concurrent")
    return sorted(({k: w.get(k) for k in keys} for w in workers), key=lambda w: w["host"])


def _question(cat: str, question: str, fmt: str, truth: dict[str, Any], source: str) -> dict:
    return {
        "category": cat,
        "question": question,
        "answer_format": fmt,
        "truth": truth,
        "source": source,
    }


HOSTS_FMT = "a comma-separated list of worker hostnames, e.g. alpha, bravo"


def build(b: Broker, rng: random.Random, now: datetime) -> dict[str, Any]:
    workers = b.get("/workers")
    jobs = sorted(b.terminal_jobs(), key=lambda j: j["id"])
    settled = [j for j in jobs if j.get("finished_at") and ts(j["finished_at"]) < now - SETTLED]
    qs: list[dict[str, Any]] = []
    used: set[int] = set()

    def draw(pool: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
        pool = [j for j in pool if j["id"] not in used]
        got = rng.sample(pool, n)
        used.update(j["id"] for j in got)
        return sorted(got, key=lambda j: j["id"])

    # Exit code: 3 completed, 4 failed (a model guessing 0 gets the 3 at most).
    with_code = [j for j in settled if j["exit_code"] is not None]
    for j in draw([j for j in with_code if j["state"] == "completed"], 3) + draw(
        [j for j in with_code if j["state"] == "failed"], 4
    ):
        qs.append(_question(
            "exit_code", f"What exit code did jobd job {j['id']} finish with?",
            "an integer, e.g. 0", {"exit_code": j["exit_code"], "job_id": j["id"]},
            f"GET /jobs/{j['id']} exit_code",
        ))  # fmt: skip

    # Host: one job per distinct worker that ever ran one, then random.
    with_host = [j for j in settled if j["worker"]]
    by_host: dict[str, list[dict[str, Any]]] = {}
    for j in with_host:
        by_host.setdefault(j["worker"], []).append(j)
    picks = [draw(by_host[h], 1)[0] for h in sorted(by_host)][:7]
    picks += draw(with_host, 7 - len(picks))
    for j in sorted(picks, key=lambda j: j["id"]):
        qs.append(_question(
            "host", f"Which worker host ran jobd job {j['id']}?", "a hostname, e.g. alpha",
            {"names": host_names(j["worker"], workers), "job_id": j["id"]},
            f"GET /jobs/{j['id']} worker, plus that host's own aliases from GET /workers",
        ))  # fmt: skip

    # Wall time: one job from each seventh of the duration distribution.
    timed = sorted(
        (j for j in settled if j["started_at"] and j["id"] not in used and duration_s(j) >= 5),
        key=duration_s,
    )
    k = len(timed) // 7
    for i in range(7):
        j = rng.choice(timed[i * k : (i + 1) * k])
        used.add(j["id"])
        qs.append(_question(
            "wall_time",
            f"How long did jobd job {j['id']} run, from its start (started_at) to its "
            "finish (finished_at)?",
            "a number of seconds, rounded to the nearest second, e.g. 754",
            {"seconds": round(duration_s(j), 3), "job_id": j["id"]},
            f"GET /jobs/{j['id']} finished_at - started_at",
        ))  # fmt: skip

    # Failures of one project on one UTC day. Projects whose names fold
    # together (Foo / foo, a-b / a_b) are left out: the broker's filter
    # matches the folded name, so the "right" set would depend on the reading.
    folded: dict[str, set[str]] = {}
    for j in jobs:
        folded.setdefault(j["project"].casefold().replace("-", "_"), set()).add(j["project"])
    clean = {p for names in folded.values() if len(names) == 1 for p in names}
    labelled = {(j["project"], j["finished_at"][:10]) for j in jobs if j.get("project_label")}
    cells: dict[tuple[str, str], list[int]] = {}
    for j in settled:
        day = ts(j["finished_at"]).astimezone(UTC).date()
        if j["state"] != "failed" or j["project"] not in clean:
            continue
        if not now.date() - FAIL_WINDOW <= day < now.date():
            continue
        cells.setdefault((j["project"], day.isoformat()), []).append(j["id"])
    fair = sorted(c for c, ids in cells.items() if 1 <= len(ids) <= 5 and c not in labelled)
    chosen: list[tuple[str, str]] = []
    for c in rng.sample(fair, len(fair)):
        if c[0] not in {p for p, _ in chosen}:
            chosen.append(c)
        if len(chosen) == 6:
            break
    for project, day in sorted(chosen, key=lambda c: (c[1], c[0])):
        qs.append(_question(
            "project_failures",
            f"Which jobd jobs of project '{project}' ended in state 'failed' with a finish "
            f"time (finished_at) on {day}, UTC? List every one.",
            "a comma-separated list of job ids, e.g. 4012, 4013",
            {"job_ids": sorted(cells[(project, day)]), "project": project, "day": day},
            f"GET /jobs?state_filter=failed, rows with project == '{project}' finished on {day} UTC",
        ))  # fmt: skip

    # Last log line of a recently finished job (older logs are pruned).
    recent = [
        j for j in settled if ts(j["finished_at"]) >= now - LOG_WINDOW and j["id"] not in used
    ]
    got = 0
    for j in rng.sample(recent, len(recent)):
        out = b.get(f"/jobs/{j['id']}/output", tail=4096)
        line = last_line(out.get("tail") or "") if not out.get("pruned") else None
        if line is None:
            continue
        used.add(j["id"])
        qs.append(_question(
            "log_line",
            f"What is the last non-empty line of the captured output (the log) of jobd job "
            f"{j['id']}? Quote it exactly.",
            "the line exactly as it appears in the log",
            {"line": line, "job_id": j["id"]},
            f"GET /jobs/{j['id']}/output, last non-empty line",
        ))  # fmt: skip
        got += 1
        if got == 7:
            break
    else:
        raise RuntimeError(f"only {got} quotable log lines among {len(recent)} recent jobs")

    qs.extend(fleet_questions(b, rng, workers))
    # Every name a worker goes by (registered hosts, their own aliases, and
    # hosts that ran a job but have since been renamed or retired), so the
    # scorer can check whether a wrong host answer names a real host.
    known = {a.lower(): w["host"] for w in workers for a in host_names(w["host"], workers)}
    known.update({j["worker"].lower(): j["worker"] for j in jobs if j["worker"]})
    for q in qs:
        if q["category"] in ("host", "fleet"):
            q["truth"]["known_names"] = dict(sorted(known.items()))
    for i, q in enumerate(qs, 1):
        q["id"] = f"q{i:02d}"
    return {
        "built": now.isoformat(timespec="seconds"),
        "seed": SEED,
        "broker_version": b.get("/health").get("version"),
        "fleet": fleet_snapshot(workers),
        "questions": qs,
    }


def fleet_questions(
    b: Broker, rng: random.Random, workers: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """5 routing questions (tag sets with 1..n-1 eligible hosts, each a
    different answer where possible) and 2 facts with a unique answer."""
    live = online(workers)
    tags = sorted({t for w in live for t in w["tags"]})
    combos = [[t] for t in tags] + [[a, c] for i, a in enumerate(tags) for c in tags[i + 1 :]]
    rng.shuffle(combos)
    out, seen = [], set()
    for needs in combos:
        hosts = eligible(needs, workers)
        if not 1 <= len(hosts) < len(live) or tuple(hosts) in seen:
            continue
        # Cross-check the documented rule against the broker's own plan; a
        # disagreement is a finding about one or the other, not a question.
        plan = b.would_route_to(needs)
        if plan != hosts:
            raise RuntimeError(f"needs={needs}: tags say {hosts}, broker dry run says {plan}")
        seen.add(tuple(hosts))
        tag_text = " and ".join(f"'{t}'" for t in needs)
        out.append(_question(
            "fleet",
            f"Which online jobd workers could run a job that needs the capability tag"
            f"{'s' if len(needs) > 1 else ''} {tag_text}, with no other requirements and no "
            "host pin? List every one.",
            HOSTS_FMT, {"hosts": hosts, "needs": needs},
            "GET /workers tags (online workers), cross-checked against POST /submit dry_run",
        ))  # fmt: skip
        if len(out) == 5:
            break
    top_tier = max(gpu_tier_gb(w) for w in live)
    tier_hosts = sorted(w["host"] for w in live if gpu_tier_gb(w) == top_tier)
    top_slots = max(w.get("max_concurrent") or 1 for w in live)
    slot_hosts = sorted(w["host"] for w in live if (w.get("max_concurrent") or 1) == top_slots)
    for hosts, text, src in (
        (tier_hosts,
         "Judging by the cuda-NNgb capability tags the online jobd workers advertise, which "
         "worker supports the largest GPU memory tier?",
         f"GET /workers: the largest cuda-NNgb tag is cuda-{top_tier}gb"),
        (slot_hosts,
         "Which online jobd worker can run the most jobs at the same time (the most "
         "concurrent job slots)?",
         f"GET /workers: max_concurrent = {top_slots}"),
    ):  # fmt: skip
        if len(hosts) == 1:
            out.append(_question("fleet", text, "a hostname, e.g. alpha", {"hosts": hosts}, src))
    return out


def drift(b: Broker, key: dict[str, Any]) -> list[str]:
    """Every way the broker no longer agrees with a frozen key; empty = safe to
    sweep. Terminal jobs cannot change, so a job difference means a row was
    pruned or rewritten; a fleet difference means a host was reconfigured or
    went offline, and the fleet answers would have moved with it."""
    problems = []
    workers = b.get("/workers")
    if fleet_snapshot(workers) != key["fleet"]:
        problems.append("fleet: workers' state/tags/aliases/slots differ from the frozen snapshot")
    for q in key["questions"]:
        t = q["truth"]
        if q["category"] == "fleet" and "needs" in t:
            if eligible(t["needs"], workers) != t["hosts"]:
                problems.append(f"{q['id']}: eligible hosts for {t['needs']} changed")
        elif "job_id" in t:
            j = b.get(f"/jobs/{t['job_id']}")
            if q["category"] == "exit_code" and j["exit_code"] != t["exit_code"]:
                problems.append(f"{q['id']}: job {t['job_id']} exit code changed")
            if q["category"] == "host" and j["worker"] != t["names"][0]:
                problems.append(f"{q['id']}: job {t['job_id']} worker changed")
            if q["category"] == "wall_time" and round(duration_s(j), 3) != t["seconds"]:
                problems.append(f"{q['id']}: job {t['job_id']} timestamps changed")
            if q["category"] == "log_line":
                out = b.get(f"/jobs/{t['job_id']}/output", tail=4096)
                if out.get("pruned") or last_line(out.get("tail") or "") != t["line"]:
                    problems.append(f"{q['id']}: job {t['job_id']} log changed or was pruned")
        elif q["category"] == "project_failures":
            rows = b.get("/jobs", state_filter="failed", project=t["project"], limit=PAGE)
            ids = sorted(
                r["id"]
                for r in rows
                if r["project"] == t["project"] and r["finished_at"][:10] == t["day"]
            )
            if ids != t["job_ids"]:
                problems.append(f"{q['id']}: failures of {t['project']} on {t['day']} changed")
    return problems


def main() -> None:
    # Armed here, not at import, so a test importing this module is not killed.
    signal.signal(signal.SIGALRM, lambda *_: sys.exit("aborting: walltime guard"))
    signal.alarm(900)
    url, token = os.environ.get("JOBD_URL"), os.environ.get("JOBD_API_TOKEN", "")
    if not url:
        sys.exit("set JOBD_URL (and JOBD_API_TOKEN) to the broker to draw from")
    b = Broker(url, token)
    key = build(b, random.Random(SEED), datetime.now(UTC))
    (HERE / "questions.json").write_text(json.dumps(key, indent=1) + "\n")
    counts: dict[str, int] = {}
    for q in key["questions"]:
        counts[q["category"]] = counts.get(q["category"], 0) + 1
    print(len(key["questions"]), counts, "frozen", key["built"])


if __name__ == "__main__":
    main()
