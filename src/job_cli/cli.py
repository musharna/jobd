"""job CLI — thin HTTP client against jobd."""
from __future__ import annotations

import json
import os
import sys

import httpx
import typer

app = typer.Typer(help="Submit and monitor jobs on jobd.")

BASE = os.environ.get("JOBD_URL", "http://10.0.0.10:8765")


def _client() -> httpx.Client:
    return httpx.Client(base_url=BASE, timeout=30.0)


@app.command()
def submit(
    cmd: list[str] = typer.Argument(..., help="Command to run"),
    project: str = typer.Option(..., "--project", "-p"),
    profile: str | None = typer.Option(None, "--profile"),
    host: str = typer.Option("any", "--host"),
    priority: int = typer.Option(0, "--priority"),
    preemptible: bool = typer.Option(False, "--preemptible"),
    cwd: str = typer.Option(os.getcwd(), "--cwd"),
    wait: bool = typer.Option(False, "--wait", "-w"),
):
    """Submit a job. With --wait, stream logs until terminal state."""
    with _client() as c:
        r = c.post(
            "/submit",
            json={
                "cmd": cmd,
                "cwd": cwd,
                "project": project,
                "profile": profile,
                "host_pin": host,
                "priority_delta": priority,
                "preemptible": preemptible,
                "session_id": os.environ.get("CLAUDE_SESSION_ID"),
            },
        )
        r.raise_for_status()
        job = r.json()
        typer.echo(json.dumps(job, default=str))
        if not wait:
            return
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
            typer.echo(f"{j['id']:>5}  {j['state']:>10}  {j['project']:>20}  {' '.join(j['cmd'])[:80]}")


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
