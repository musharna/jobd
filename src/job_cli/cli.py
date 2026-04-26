"""job CLI — thin HTTP client against jobd."""

from __future__ import annotations

import json
import os
import sys
import time

import httpx
import typer
from jobd.client import JobdClient

app = typer.Typer(help="Submit and monitor jobs on jobd.")

BASE = os.environ.get("JOBD_URL", "http://100.113.204.41:8765")
TERMINAL_STATES = {"completed", "failed", "cancelled"}


def _client() -> JobdClient:
    return JobdClient(base_url=BASE)


@app.command()
def submit(
    cmd: list[str] = typer.Argument(..., help="Command to run"),
    project: str = typer.Option(..., "--project", "-p"),
    profile: str | None = typer.Option(None, "--profile"),
    host: str = typer.Option("any", "--host"),
    priority_delta: int = typer.Option(0, "--priority-delta"),
    preemptible: bool = typer.Option(False, "--preemptible"),
    cwd: str = typer.Option(lambda: os.getcwd(), "--cwd"),
    wait: bool = typer.Option(False, "--wait", "-w"),
    needs: list[str] = typer.Option(None, "--needs", help="required capability tag (repeatable)"),
    arch: str = typer.Option("any", "--arch", help="required worker arch (any|x86_64|arm64|arm7)"),
    os_: str = typer.Option("any", "--os", help="required worker OS (any|linux|darwin|windows)"),
    gpu: bool | None = typer.Option(
        None,
        "--gpu/--no-gpu",
        help="require GPU (--gpu) / forbid GPU (--no-gpu) / don't care (default)",
    ),
    idempotent: bool = typer.Option(
        False, "--idempotent", help="reclaim orphaned run after 90s instead of 5min"
    ),
    session_id: str | None = typer.Option(
        None, "--session-id", help="tag job with a session id (defaults to $CLAUDE_SESSION_ID)"
    ),
    depends_on: list[int] = typer.Option(
        None, "--depends-on", help="parent job id that must complete first (repeatable)"
    ),
    depends_on_any_exit: bool = typer.Option(
        False,
        "--depends-on-any-exit",
        help="unblock when parent reaches any terminal state, not just completed",
    ),
):
    """Submit a job. With --wait, stream logs until terminal state."""
    if session_id is None:
        session_id = os.environ.get("CLAUDE_SESSION_ID")
    requires: dict | None = None
    if needs or arch != "any" or os_ != "any" or gpu is not None or idempotent:
        requires = {
            "arch": arch,
            "os": os_,
            "gpu": gpu,
            "needs": list(needs or []),
            "idempotent": idempotent,
        }
    body: dict = {
        "cmd": cmd,
        "cwd": cwd,
        "project": project,
        "profile": profile,
        "host_pin": host,
        "priority_delta": priority_delta,
        "preemptible": preemptible,
        "session_id": session_id,
    }
    if requires is not None:
        body["requires"] = requires
    if depends_on:
        body["depends_on"] = list(depends_on)
    if depends_on_any_exit:
        body["depends_on_any_exit"] = True
    r = httpx.post(f"{BASE}/submit", json=body)
    if r.is_error:
        r.raise_for_status()
    job = r.json()
    typer.echo(json.dumps(job, default=str))
    if wait:
        _stream_wait(job["id"])


def _stream_wait(job_id: int) -> None:
    with httpx.stream("GET", f"{BASE}/wait/{job_id}", timeout=None) as s:
        exit_code = 0
        for line in s.iter_lines():
            if not line:
                continue
            if line.startswith("event: log"):
                continue
            if line.startswith("data: "):
                data = line[6:]
                try:
                    parsed = json.loads(data)
                    if "state" in parsed:
                        exit_code = parsed.get("exit_code") or 0
                        typer.echo(f"[terminal] {parsed}", err=True)
                        break
                except json.JSONDecodeError:
                    typer.echo(data)
        sys.exit(exit_code)


STALE_HEARTBEAT_SECONDS = 60


def _worker_health_banner(client: httpx.Client) -> None:
    """Emit a one-line banner if any worker is offline or stale. Silent when healthy."""
    from datetime import datetime, timezone

    try:
        r = client.get("/workers")
        r.raise_for_status()
    except (httpx.HTTPError, httpx.ConnectError):
        return
    workers = r.json()
    if not workers:
        typer.secho("\u26a0 no workers registered \u2014 nothing will dispatch", fg="yellow")
        return
    now = datetime.now(timezone.utc)
    bad: list[str] = []
    for w in workers:
        hb = w.get("last_heartbeat")
        if w.get("state") == "offline":
            bad.append(f"{w['host']} (offline)")
            continue
        if not hb:
            continue
        try:
            age = (now - datetime.fromisoformat(hb)).total_seconds()
        except ValueError:
            continue
        if age > STALE_HEARTBEAT_SECONDS:
            bad.append(f"{w['host']} (stale {int(age)}s)")
    if bad:
        typer.secho(f"\u26a0 worker health: {', '.join(bad)}", fg="yellow")


@app.command(name="list")
def list_jobs(
    state: str | None = typer.Option(None),
    project: str | None = typer.Option(None),
):
    """List jobs."""
    with _client() as c:
        _worker_health_banner(c)
        params = {}
        if state:
            params["state_filter"] = state
        if project:
            params["project"] = project
        r = c.get("/jobs", params=params)
        r.raise_for_status()
        jobs = r.json()
        state_by_id = {j["id"]: j["state"] for j in jobs}
        for j in jobs:
            typer.echo(
                f"{j['id']:>5}  {j['state']:>10}  {j['project']:>20}  {' '.join(j['cmd'])[:80]}"
            )
            deps = j.get("depends_on") or []
            if deps:
                parts = []
                for d in deps:
                    st = state_by_id.get(d)
                    mark = (
                        "\u2713"
                        if st == "completed"
                        else (
                            "\u2717"
                            if st in {"failed", "cancelled", "preempted", "orphaned"}
                            else "\u29d6"
                        )
                    )
                    parts.append(f"{d}{mark}")
                typer.echo(f"  deps: {' '.join(parts)}")
            if j.get("warning"):
                typer.secho(f"  \u26a0 {j['warning']}", fg="yellow")


def _render_status(j: dict) -> str:
    lines = [
        f"job {j['id']}  state={j['state']}  project={j['project']}  priority={j['priority']}",
        f"  host_pin={j['host_pin']}  worker={j.get('worker') or '-'}  preemptible={j.get('preemptible', False)}",
        f"  submitted={j.get('submitted_at') or '-'}",
        f"  started  ={j.get('started_at') or '-'}",
        f"  finished ={j.get('finished_at') or '-'}",
        f"  exit_code={j.get('exit_code')}",
        f"  cmd: {' '.join(j['cmd'])[:100]}",
    ]
    if j.get("warning"):
        lines.append(f"  ⚠ {j['warning']}")
    return "\n".join(lines)


@app.command()
def status(
    job_id: int,
    watch: bool = typer.Option(False, "--watch", "-w", help="poll until terminal"),
    interval: float = typer.Option(2.0, "--interval", help="poll interval seconds (with --watch)"),
):
    """Print one job's state. With --watch, redraw until terminal."""
    with _client() as c:
        if not watch:
            r = c.get(f"/jobs/{job_id}")
            j = r.json()
            typer.echo(_render_status(j))
            sys.exit(j.get("exit_code") or 0 if j["state"] in TERMINAL_STATES else 0)
        try:
            while True:
                r = c.get(f"/jobs/{job_id}")
                j = r.json()
                sys.stdout.write("\x1b[2J\x1b[H")
                sys.stdout.write(_render_status(j) + "\n")
                sys.stdout.flush()
                if j["state"] in TERMINAL_STATES:
                    sys.exit(j.get("exit_code") or 0)
                time.sleep(interval)
        except KeyboardInterrupt:
            sys.exit(130)


@app.command()
def cancel(job_id: int):
    with _client() as c:
        r = c.post(f"/jobs/{job_id}/cancel")
        r.raise_for_status()
        typer.echo(json.dumps(r.json(), default=str))


@app.command()
def wait(job_id: int):
    _stream_wait(job_id)


@app.command()
def logs(
    job_id: int,
    tail: int = typer.Option(8192, "--tail", "-n", help="bytes to show from end of log"),
):
    """Print the tail of a job's captured stdout+stderr. Handy for post-mortem
    on a finished job without SSHing to the worker."""
    with _client() as c:
        r = c.get(f"/jobs/{job_id}/output", params={"tail": tail})
        r.raise_for_status()
        body = r.json()
        if body["size_bytes"] == 0:
            typer.secho(f"[no log captured for job {job_id}]", fg="yellow")
            return
        if body["truncated"]:
            typer.secho(
                f"[truncated: showing last {body['returned_bytes']} of {body['size_bytes']} bytes]",
                fg="yellow",
                err=True,
            )
        typer.echo(body["tail"], nl=False)


@app.command()
def classify(cmd: str):
    with _client() as c:
        r = c.post("/classify", json={"cmd": cmd})
        r.raise_for_status()
        typer.echo(json.dumps(r.json(), indent=2))


projects_app = typer.Typer(help="Manage project priorities.")
app.add_typer(projects_app, name="projects")


@projects_app.command("list")
def projects_list():
    with _client() as c:
        r = c.get("/projects")
        for name, pri in sorted(r.json().items(), key=lambda kv: -kv[1]):
            typer.echo(f"{name:>30}  {pri:>3}")


@projects_app.command("set")
def projects_set(name: str, priority: int):
    with _client() as c:
        r = c.post(f"/projects/{name}", json={"priority": priority})
        r.raise_for_status()
        typer.echo(f"{name} -> {r.json()[name]}")


@projects_app.command("nudge")
def projects_nudge(name: str, delta: int):
    with _client() as c:
        r = c.post(f"/projects/{name}/nudge", json={"delta": delta})
        r.raise_for_status()
        typer.echo(f"{name} -> {r.json()[name]}")


if __name__ == "__main__":
    app()
