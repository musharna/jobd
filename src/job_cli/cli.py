"""job CLI — thin HTTP client against jobd."""

from __future__ import annotations

import json
import os
import sys

import httpx
import typer

app = typer.Typer(help="Submit and monitor jobs on jobd.")

BASE = os.environ.get("JOBD_URL", "http://100.113.204.41:8765")


def _client() -> httpx.Client:
    return httpx.Client(base_url=BASE, timeout=30.0)


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


@app.command(name="list")
def list_jobs(
    state: str | None = typer.Option(None),
    project: str | None = typer.Option(None),
):
    """List jobs."""
    with _client() as c:
        params = {}
        if state:
            params["state_filter"] = state
        if project:
            params["project"] = project
        r = c.get("/jobs", params=params)
        r.raise_for_status()
        for j in r.json():
            typer.echo(
                f"{j['id']:>5}  {j['state']:>10}  {j['project']:>20}  {' '.join(j['cmd'])[:80]}"
            )
            if j.get("warning"):
                typer.secho(f"  \u26a0 {j['warning']}", fg="yellow")


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
