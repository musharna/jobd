"""Fixed fleet + job history, and the workloads the perf ratchet counts.

Everything here runs against an in-process broker (``build_app`` + Starlette's
``TestClient``) on a fresh SQLite file. Nothing opens a socket: ``ratchet.py``
replaces ``socket.socket.connect`` with a tripwire before any workload runs, so
a workload that reached for a real broker (the live fleet included) would fail
loudly instead of counting someone else's work.

The fixture is drawn from ``random.Random(SEED)``: four workers, then
``N_JOBS`` submissions with seeded requirements, of which the first
``N_FINISHED`` are dispatched through ``/next-job``, given a log, and completed
or failed. The rest stay queued, so a scheduling pass has a real queue to scan.

Each workload returns a *witness*: a value only that workload's real output
produces (ids, counts, a hash of the reply with timestamps removed). The
ratchet compares it with the baseline, so a workload that silently ran
something else (a 404, a default branch) fails on the witness, not just on
the counts.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from jobd.app import build_app

SEED = 20261016
N_JOBS = 40
N_FINISHED = 28

PROJECTS_YAML = """projects:
  project-b: { priority: 80 }
  project-a: { priority: 55 }
  _default: { priority: 40 }
"""
PROFILES_YAML = """profiles:
  small:
    vram_gb: 0
    ram_gb: 2
    cpus: 2
    expected_runtime: 10m
    preemptible: true
    host_hint: any
    fast_path: true
"""
CLASSIFIER_YAML = "rules: []\n"

# Neutral host names, shaped like a real four-host fleet: one big GPU, two
# mid GPUs (one with three slots), one CPU-only box.
WORKERS: list[dict[str, Any]] = [
    {
        "host": "alpha",
        "host_aliases": ["any", "any-gpu", "alpha-wsl"],
        "free_vram_gb": 31.0,
        "unregistered_vram_gb": 0.0,
        "free_ram_gb": 28.0,
        "idle_cpus": 20,
        "arch": "x86_64",
        "os": "linux",
        "gpu": True,
        "tags": ["python3", "R", "cuda", "cuda-12gb", "cuda-24gb", "cuda-32gb"],
        "mount_roots": ["/home", "/tmp"],
        "max_concurrent": 1,
        "running": 0,
    },
    {
        "host": "bravo",
        "host_aliases": ["any", "any-gpu"],
        "free_vram_gb": 7.5,
        "unregistered_vram_gb": 0.0,
        "free_ram_gb": 50.0,
        "idle_cpus": 16,
        "arch": "x86_64",
        "os": "linux",
        "gpu": True,
        "tags": ["python3", "R", "docker", "cuda", "cuda-8gb"],
        "mount_roots": ["/home", "/tmp"],
        "max_concurrent": 1,
        "running": 0,
    },
    {
        "host": "charlie",
        "host_aliases": ["any", "any-gpu"],
        "free_vram_gb": 23.0,
        "unregistered_vram_gb": 0.0,
        "free_ram_gb": 20.0,
        "idle_cpus": 12,
        "arch": "x86_64",
        "os": "linux",
        "gpu": True,
        "tags": ["python3", "ffmpeg", "cuda", "cuda-8gb", "cuda-24gb"],
        "mount_roots": ["/home", "/tmp", "/mnt/c"],
        "max_concurrent": 3,
        "running": 0,
    },
    {
        "host": "delta",
        "host_aliases": ["any"],
        "free_vram_gb": 0.0,
        "unregistered_vram_gb": 0.0,
        "free_ram_gb": 14.0,
        "idle_cpus": 8,
        "arch": "x86_64",
        "os": "linux",
        "gpu": False,
        "tags": ["python3", "R"],
        "mount_roots": ["/home", "/tmp"],
        "max_concurrent": 1,
        "running": 0,
    },
]

# (needs, gpu, host_pin) a submission draws from.
_REQUIREMENTS: list[tuple[list[str], bool, str]] = [
    ([], False, "any"),
    (["R"], False, "any"),
    (["cuda"], True, "any"),
    (["cuda-24gb"], True, "any"),
    (["cuda-32gb"], True, "any"),
    (["ffmpeg"], False, "any"),
    ([], False, "delta"),
    (["python3"], False, "bravo"),
]


@dataclass
class Fixture:
    client: TestClient
    app: Any
    job_ids: list[int]
    finished: list[int]
    queued: list[int]


def _submit_body(rng: random.Random, i: int) -> dict[str, Any]:
    needs, gpu, pin = rng.choice(_REQUIREMENTS)
    body: dict[str, Any] = {
        "cmd": ["python3", "train.py", f"--seed={i}"],
        "cwd": "/tmp/work",
        "project": rng.choice(["project-a", "project-b"]),
        "host_pin": pin,
        "max_wall_s": 3600,
    }
    # gpu is a selector, not a hint: "gpu": false means "a host WITHOUT a GPU",
    # so it is sent only when a GPU is wanted.
    if needs or gpu:
        body["requires"] = {"needs": needs, **({"gpu": True} if gpu else {})}
    return body


def heartbeat(client: TestClient, w: dict[str, Any]) -> None:
    r = client.post("/heartbeat", json=w)
    r.raise_for_status()


def _open(tmp: Path) -> tuple[Any, TestClient]:
    app = build_app(
        db_url=f"sqlite:///{tmp}/jobd.db",
        projects_path=tmp / "projects.yaml",
        profiles_path=tmp / "profiles.yaml",
        classifier_path=tmp / "classifier.yaml",
        logs_path=tmp / "logs",
        project_overrides_path=tmp / "project-priorities.yaml",
    )
    # No `with`: entering the context would run the lifespan, which starts the
    # background sweep loop, and its timer-driven work would leak into counts.
    return app, TestClient(app)


def build_template(tmp: Path) -> None:
    """Populate a broker in ``tmp`` deterministically, then close it.

    Built once per measurement; every workload run gets its own copy
    (``open_fixture``), so all of them start from byte-identical state and
    the ~200 setup requests are not paid 2x per workload."""
    tmp.mkdir(parents=True, exist_ok=True)
    for name, text in (
        ("projects.yaml", PROJECTS_YAML),
        ("profiles.yaml", PROFILES_YAML),
        ("classifier.yaml", CLASSIFIER_YAML),
    ):
        (tmp / name).write_text(text)
    app, client = _open(tmp)
    rng = random.Random(SEED)
    for w in WORKERS:
        heartbeat(client, w)
    job_ids: list[int] = []
    for i in range(N_JOBS):
        r = client.post("/submit", json=_submit_body(rng, i))
        r.raise_for_status()
        job_ids.append(r.json()["id"])
    finished: list[int] = []
    # Dispatch round-robin over the workers until N_FINISHED jobs ran.
    idle_rounds = 0
    while len(finished) < N_FINISHED and idle_rounds < len(WORKERS):
        progressed = False
        for w in WORKERS:
            if len(finished) >= N_FINISHED:
                break
            claim = client.post("/next-job", json=_query(w))
            claim.raise_for_status()
            job = claim.json()
            if not job:
                continue
            progressed = True
            jid = job["id"]
            client.post(f"/jobs/{jid}/started").raise_for_status()
            lines = "".join(
                f"step {k} loss {rng.random():.4f}\n" for k in range(rng.randint(3, 12))
            )
            client.post(f"/jobs/{jid}/log", content=lines.encode()).raise_for_status()
            code = 0 if rng.random() < 0.7 else rng.choice([1, 2, 137])
            client.post(
                f"/jobs/{jid}/complete",
                json={"exit_code": code, "final_state": "completed" if code == 0 else "failed"},
            ).raise_for_status()
            finished.append(jid)
        idle_rounds = 0 if progressed else idle_rounds + 1
    if len(finished) != N_FINISHED:
        raise RuntimeError(f"fixture finished {len(finished)} of {N_FINISHED} jobs")
    # Re-heartbeat so every worker reads as idle and freshly seen.
    for w in WORKERS:
        heartbeat(client, w)
    client.close()
    app.state.engine.dispose()  # checkpoints the WAL into jobd.db before the copy
    (tmp / "ids.json").write_text(json.dumps({"job_ids": job_ids, "finished": finished}))


def open_fixture(template: Path, dest: Path) -> Fixture:
    """A private copy of the template broker, opened."""
    shutil.copytree(template, dest)
    ids = json.loads((dest / "ids.json").read_text())
    app, client = _open(dest)
    # Heartbeat again on every copy: the template's heartbeats age while the
    # measurement runs, and a worker that crossed the staleness threshold
    # would change both the counts and the witness of later workloads.
    for w in WORKERS:
        heartbeat(client, w)
    finished = ids["finished"]
    queued = [j for j in ids["job_ids"] if j not in finished]
    return Fixture(client=client, app=app, job_ids=ids["job_ids"], finished=finished, queued=queued)


def _query(w: dict[str, Any]) -> dict[str, Any]:
    keys = ("host", "free_vram_gb", "unregistered_vram_gb", "free_ram_gb", "idle_cpus",
            "arch", "os", "gpu", "tags", "mount_roots")  # fmt: skip
    return {k: w[k] for k in keys}


_VOLATILE = re.compile(
    r"(_at|heartbeat|^ts$|^since$|eta_|^duration_s$|^ms$|^seconds$|pruned)", re.IGNORECASE
)


def _stable(obj: Any) -> Any:
    """The reply with clock-derived fields removed, so a hash of it is stable."""
    if isinstance(obj, dict):
        return {k: _stable(v) for k, v in sorted(obj.items()) if not _VOLATILE.search(k)}
    if isinstance(obj, list):
        return [_stable(v) for v in obj]
    return obj


def digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(_stable(obj), sort_keys=True).encode()).hexdigest()[:16]


def _ok(r: Any) -> Any:
    r.raise_for_status()
    return r.json()


def _mcp(fx: Fixture) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """jobd-mcp's own dispatch (schema validation + tool + error mapping), over
    a JobdClient whose transport is the in-process broker."""
    from jobd.client import JobdClient
    from jobd.mcp.server import build_server

    jc = JobdClient(base_url="http://testserver")
    jc._client = fx.client  # TestClient is an httpx.Client: same request path
    dispatch = build_server(jc)._jobd_dispatch  # type: ignore[attr-defined]
    return dispatch  # type: ignore[no-any-return]


def _probe_job(fx: Fixture) -> int:
    """A finished job with a log, chosen by position, not by id arithmetic."""
    return fx.finished[len(fx.finished) // 2]


_NEW_JOB = {
    "cmd": ["python3", "eval.py"],
    "cwd": "/tmp/work",
    "project": "project-a",
    "host_pin": "any",
    "requires": {"needs": ["cuda-24gb"], "gpu": True},
}


def w_api_submit(fx: Fixture) -> Any:
    j = _ok(fx.client.post("/submit", json=_NEW_JOB))
    return {"id": j["id"], "state": j["state"], "project": j["project"]}


def w_api_submit_dry_run(fx: Fixture) -> Any:
    plan = _ok(fx.client.post("/submit", json={**_NEW_JOB, "dry_run": True}))
    return {"state": plan["state"], "would_route_to": plan["would_route_to"]}


def w_api_list_jobs(fx: Fixture) -> Any:
    rows = _ok(fx.client.get("/jobs", params={"limit": 50}))
    return {"n": len(rows), "ids": digest([r["id"] for r in rows])}


def w_api_list_failed_for_project(fx: Fixture) -> Any:
    rows = _ok(
        fx.client.get(
            "/jobs", params={"state_filter": "failed", "project": "project-a", "limit": 50}
        )
    )
    return {"ids": sorted(r["id"] for r in rows)}


def w_api_job_status(fx: Fixture) -> Any:
    j = _ok(fx.client.get(f"/jobs/{_probe_job(fx)}"))
    return {"id": j["id"], "state": j["state"], "worker": j["worker"], "exit": j["exit_code"]}


def w_api_job_output(fx: Fixture) -> Any:
    o = _ok(fx.client.get(f"/jobs/{_probe_job(fx)}/output", params={"tail": 8192}))
    return {"size": o["size_bytes"], "tail": digest(o["tail"])}


def w_api_workers(fx: Fixture) -> Any:
    ws = _ok(fx.client.get("/workers"))
    return sorted((w["host"], w["state"], len(w["tags"])) for w in ws)


def w_api_events(fx: Fixture) -> Any:
    ev = _ok(fx.client.get("/events", params={"limit": 200}))
    return {"n": len(ev), "kinds": digest(sorted(e.get("event") for e in ev))}


def w_api_heartbeat(fx: Fixture) -> Any:
    heartbeat(fx.client, WORKERS[2])
    w = next(x for x in _ok(fx.client.get("/workers")) if x["host"] == "charlie")
    return {"host": w["host"], "state": w["state"]}


def w_sched_pass(fx: Fixture) -> Any:
    """One scheduling pass: every worker asks /next-job once, in fleet order."""
    got = []
    for w in WORKERS:
        job = _ok(fx.client.post("/next-job", json=_query(w)))
        got.append((w["host"], job["id"] if job else None))
    return got


def w_mcp_status(fx: Fixture) -> Any:
    out = _mcp(fx)("jobd_status", {"job_id": _probe_job(fx)})
    return {"job_id": out["job_id"], "state": out["state"], "host": out["host"]}


def w_mcp_logs(fx: Fixture) -> Any:
    out = _mcp(fx)("jobd_logs", {"job_id": _probe_job(fx)})
    return {"size": out["size_bytes"], "tail": digest(out["log_tail"])}


def w_mcp_list_default(fx: Fixture) -> Any:
    out = _mcp(fx)("jobd_list", {})
    return {"counts": out["counts"], "ids": [j["job_id"] for j in out["jobs"]]}


def w_mcp_list_project_failed(fx: Fixture) -> Any:
    out = _mcp(fx)("jobd_list", {"state": ["failed"], "project": "project-a", "limit": 200})
    return {"counts": out["counts"], "ids": sorted(j["job_id"] for j in out["jobs"])}


def w_mcp_workers(fx: Fixture) -> Any:
    out = _mcp(fx)("jobd_workers", {})
    return {"health": out["fleet_health"], "hosts": [w["host"] for w in out["workers"]]}


def w_mcp_events(fx: Fixture) -> Any:
    out = _mcp(fx)("jobd_events", {"job_id": _probe_job(fx)})
    return {"kinds": [e.get("event") for e in out["events"]]}


def w_mcp_submit_dry_run(fx: Fixture) -> Any:
    out = _mcp(fx)(
        "jobd_submit",
        {
            "command": "python3 eval.py",
            "project": "project-a",
            "cwd": "/tmp/work",
            "needs": ["cuda-24gb"],
            "gpu": True,
            "dry_run": True,
        },
    )
    return {"state": out["state"], "would_route_to": out["would_route_to"]}


def w_mcp_submit(fx: Fixture) -> Any:
    out = _mcp(fx)(
        "jobd_submit",
        {"command": "python3 eval.py", "project": "project-a", "cwd": "/tmp/work"},
    )
    return {"job_id": out["job_id"], "state": out["state"]}


WORKLOADS: dict[str, Callable[[Fixture], Any]] = {
    "api_submit": w_api_submit,
    "api_submit_dry_run": w_api_submit_dry_run,
    "api_list_jobs": w_api_list_jobs,
    "api_list_failed_for_project": w_api_list_failed_for_project,
    "api_job_status": w_api_job_status,
    "api_job_output": w_api_job_output,
    "api_workers": w_api_workers,
    "api_events": w_api_events,
    "api_heartbeat": w_api_heartbeat,
    "sched_pass": w_sched_pass,
    "mcp_jobd_status": w_mcp_status,
    "mcp_jobd_logs": w_mcp_logs,
    "mcp_jobd_list_default": w_mcp_list_default,
    "mcp_jobd_list_project_failed": w_mcp_list_project_failed,
    "mcp_jobd_workers": w_mcp_workers,
    "mcp_jobd_events": w_mcp_events,
    "mcp_jobd_submit_dry_run": w_mcp_submit_dry_run,
    "mcp_jobd_submit": w_mcp_submit,
}
