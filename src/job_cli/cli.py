"""job CLI — thin HTTP client against jobd."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import typer
from jobd.client import BrokerRefusal, BrokerServerError, BrokerUnreachable, JobdClient

app = typer.Typer(help="Submit and monitor jobs on jobd.")

BASE = os.environ.get("JOBD_URL", "http://127.0.0.1:8765")
TERMINAL_STATES = {"completed", "failed", "cancelled"}
TERMINAL_FAIL_STATES = {"failed", "cancelled", "preempted", "orphaned"}


def _client() -> JobdClient:
    return JobdClient(base_url=BASE)


@app.command()
def submit(
    cmd: list[str] = typer.Argument(..., help="Command to run"),
    project: str = typer.Option(..., "--project", "-p"),
    profile: str | None = typer.Option(None, "--profile"),
    host: str = typer.Option("any", "--host"),
    priority_delta: int = typer.Option(0, "--priority-delta"),
    preemptible: bool | None = typer.Option(
        None,
        "--preemptible/--no-preemptible",
        help="mark job preemptible (--preemptible) / non-preemptible (--no-preemptible) / "
        "fall through to project default (omit)",
    ),
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
    max_wall: int | None = typer.Option(
        None,
        "--max-wall",
        help="kill job after this many seconds of wall-clock (1..604800)",
    ),
    idle_timeout: int | None = typer.Option(
        None,
        "--idle-timeout",
        help="kill job after this many seconds with no stdout/stderr output (1..86400)",
    ),
    checkpoint_grace: int | None = typer.Option(
        None,
        "--checkpoint-grace",
        help="seconds the workload has to checkpoint after SIGTERM during a "
        "preempt before SIGKILL (1..300, default 60)",
    ),
    scheduling_timeout_s: int | None = typer.Option(
        None,
        "--scheduling-timeout-s",
        help="cap how long the job may sit queued before the broker auto-"
        "terminates it as scheduling_timeout (1..604800). Pattern from "
        "Hatchet; use for short capability-mismatch smokes that would "
        "otherwise queue forever.",
    ),
    vram_required: float | None = typer.Option(
        None,
        "--vram-required",
        help="GB of GPU VRAM the job needs at dispatch. Resolution: this flag > "
        "max cuda-Ngb tier tag in --needs > 2 GB implicit floor for --gpu jobs. "
        "Append `# CONCURRENT_OK` to the command to bypass the worker-side "
        "live VRAM gate (matcher placement decision still applies).",
    ),
    explain: bool = typer.Option(
        False,
        "--explain",
        help="print resolved config (project defaults / profile / globals applied) "
        "without submitting",
    ),
    eta: bool = typer.Option(
        True,
        "--eta/--no-eta",
        help="print a one-line ETA banner after submit (p50/p90 from history, "
        "or ColdStart on insufficient history). On by default; --no-eta suppresses.",
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
        "session_id": session_id,
        "submitted_via": "cli",
    }
    # Only include preemptible if the user explicitly passed --preemptible /
    # --no-preemptible. Sending nothing lets the broker fall through to the
    # project default per docs/plans/projects-yaml.md §3. Sending an explicit
    # bool still wins.
    if preemptible is not None:
        body["preemptible"] = preemptible
    if requires is not None:
        body["requires"] = requires
    if depends_on:
        body["depends_on"] = list(depends_on)
    if depends_on_any_exit:
        body["depends_on_any_exit"] = True
    if max_wall is not None:
        body["max_wall_s"] = max_wall
    if idle_timeout is not None:
        body["idle_timeout_s"] = idle_timeout
    if checkpoint_grace is not None:
        body["checkpoint_grace_s"] = checkpoint_grace
    if scheduling_timeout_s is not None:
        body["scheduling_timeout_s"] = scheduling_timeout_s
    if vram_required is not None:
        body["vram_gb"] = vram_required
    if explain:
        with _client() as c:
            r = c.post("/resolve", json=body)
        _print_resolved(r.json())
        return
    with _client() as c:
        r = c.post("/submit", json=body)
    job = r.json()
    typer.echo(json.dumps(job, default=str))
    if eta:
        banner = _eta_banner_line(job)
        if banner:
            typer.echo(banner, err=True)
    if wait:
        _stream_wait(job["id"])


def _fmt_seconds(s: int | None) -> str:
    if s is None:
        return "null"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{s} ({h}h{m}m{sec}s)"


def _fmt_duration_short(s: float | None) -> str | None:
    """Render seconds as '42m' / '1h05m' / '2d3h'. None → None."""
    if s is None:
        return None
    s = max(0, int(round(s)))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        h, rem = divmod(s, 3600)
        return f"{h}h{rem // 60:02d}m"
    d, rem = divmod(s, 86400)
    return f"{d}d{rem // 3600}h"


def _eta_banner_line(j: dict) -> str | None:
    """One-line ETA banner for `job submit` default-on rendering.

    Returns:
      - 'Estimated wall p50 …, p90 … (n=N prior runs)' when history is present
        (eta_basis = history-N=k with k ≥ MIN_SAMPLES).
      - 'ColdStart: insufficient history (n=k)' when history is too thin.
      - None when the broker reported no eta_basis at all (defensive).
    """
    basis = j.get("eta_basis")
    if not basis:
        return None
    if basis.startswith("history-N="):
        p50 = j.get("eta_p50_s")
        p90 = j.get("eta_p90_s")
        if p50 is None or p90 is None:
            return None
        n = basis.split("=", 1)[1]
        line = (
            f"Estimated wall p50 {_fmt_duration_short(p50)}, "
            f"p90 {_fmt_duration_short(p90)} (n={n} prior runs)"
        )
        if j.get("eta_clipped"):
            line += " ⚠ history max-wall-clipped"
        return line
    if basis.startswith("insufficient-history-N="):
        n = basis.split("=", 1)[1]
        return f"ColdStart: insufficient history (n={n})"
    if basis.startswith("ctest-cost-K="):
        p50 = j.get("eta_p50_s")
        if p50 is None:
            return None
        k = basis.split("=", 1)[1]
        return f"Estimated wall ~{_fmt_duration_short(p50)} (ctest cost-data, k={k} tests)"
    return None


def _eta_lines(j: dict) -> list[str]:
    """ETA lines for status/list/submit display.

    Display gates per BACKLOG.md spec:
      - running: show remaining ETA only when elapsed ≥ max(60s, 10% of p50)
      - queued/assigned: show start ETA + predicted total when available
      - cold-start bucket (eta_basis = insufficient-history-*): show that
        once, so users learn why no number appeared
    """
    lines: list[str] = []
    state = j.get("state")
    p50 = j.get("eta_p50_s")
    p90 = j.get("eta_p90_s")
    basis = j.get("eta_basis")
    clipped = j.get("eta_clipped")

    if state == "running":
        rem_p50 = j.get("eta_remaining_p50_s")
        rem_p90 = j.get("eta_remaining_p90_s")
        if rem_p50 is not None and p50 is not None:
            from datetime import datetime as _dt

            try:
                started = _dt.fromisoformat(j["started_at"])
                elapsed = (_dt.now(started.tzinfo) - started).total_seconds()
            except Exception:
                elapsed = None
            gate_ok = elapsed is not None and elapsed >= max(60.0, 0.1 * p50)
            if gate_ok:
                line = f"  eta: ~{_fmt_duration_short(rem_p50)} left (p90 ~{_fmt_duration_short(rem_p90)})"
                if clipped:
                    line += "  ⚠ history max-wall-clipped"
                lines.append(line)
                lines.append(f"  eta_basis: {basis}")
        elif basis and basis.startswith("insufficient-history"):
            lines.append(f"  eta: insufficient history ({basis})")
    elif state in ("queued", "assigned"):
        start = j.get("eta_start_p50_s")
        if p50 is not None:
            seg = [f"projected wall ~{_fmt_duration_short(p50)} (p90 ~{_fmt_duration_short(p90)})"]
            if start is not None:
                seg.insert(0, f"start in ~{_fmt_duration_short(start)}")
                if start > 0:
                    seg.append(f"finish in ~{_fmt_duration_short(start + p50)}")
            line = "  eta: " + " · ".join(seg)
            if clipped:
                line += "  ⚠ history max-wall-clipped"
            lines.append(line)
            lines.append(f"  eta_basis: {basis}")
        elif basis and basis.startswith("insufficient-history"):
            lines.append(f"  eta: insufficient history ({basis})")
    return lines


def _fmt_requires(req: dict | None) -> str:
    if not req:
        return "null"
    parts = []
    if req.get("gpu") is not None:
        parts.append(f"gpu={str(req['gpu']).lower()}")
    needs = req.get("needs") or []
    if needs:
        parts.append(f"needs={list(needs)}")
    arch = req.get("arch")
    if arch and arch != "any":
        parts.append(f"arch={arch}")
    os_v = req.get("os")
    if os_v and os_v != "any":
        parts.append(f"os={os_v}")
    if req.get("idempotent"):
        parts.append("idempotent=true")
    return " ".join(parts) if parts else "null"


_SOURCE_LABEL = {
    "cli": "cli flag",
    "project_default": "project default",
    "profile": "profile",
    "global": "global",
}


def _print_resolved(resolved: dict) -> None:
    """Pretty-print a ResolvedConfig payload from POST /resolve."""
    typer.echo(f"resolved config for project {resolved['project']}:")
    rows: list[tuple[str, str, str]] = []

    def _row(label: str, fr: dict, fmt: Callable[[Any], str] = str) -> None:
        val = fr["value"]
        rendered = fmt(val) if val is not None else "null"
        raw_source = fr["source"]
        source = _SOURCE_LABEL.get(raw_source) or str(raw_source)
        rows.append((label, rendered, source))

    _row("priority", resolved["effective_priority"])
    _row("host_pin", resolved["effective_host_pin"])
    _row("max_wall_s", resolved["effective_max_wall_s"], _fmt_seconds)
    _row("idle_timeout_s", resolved["effective_idle_timeout_s"], _fmt_seconds)
    _row("checkpoint_grace_s", resolved["effective_checkpoint_grace_s"], _fmt_seconds)
    _row(
        "preemptible",
        resolved["effective_preemptible"],
        lambda v: "true" if v else "false",
    )
    _row("requires", resolved["effective_requires"], _fmt_requires)
    _row(
        "escalate_to_arc",
        resolved["effective_escalate_to_arc"],
        lambda v: "true" if v else "false",
    )
    width = max(len(label) for label, _, _ in rows)
    for label, rendered, source in rows:
        typer.echo(f"  {label:<{width}}  {rendered:<24}  [source: {source}]")
    sw = resolved.get("submit_warning")
    typer.echo(f"  submit_warning: {sw if sw else 'none'}")


def _stream_wait(job_id: int) -> None:
    with _client() as c, c.stream("GET", f"/wait/{job_id}", timeout=None) as s:
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


def _worker_health_banner(client: JobdClient) -> None:
    """Emit a one-line banner if any worker is offline or stale. Silent when healthy."""

    try:
        r = client.get("/workers")
    except (BrokerUnreachable, BrokerServerError, BrokerRefusal):
        return
    workers = r.json()
    if not workers:
        typer.secho("⚠ no workers registered — nothing will dispatch", fg="yellow")
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
        typer.secho(f"⚠ worker health: {', '.join(bad)}", fg="yellow")


@app.command(name="list")
def list_jobs(
    state: str | None = typer.Option(None),
    project: str | None = typer.Option(None),
    warnings: bool = typer.Option(
        False, "--warnings", "-w", help="Show only jobs with a non-null warning."
    ),
):
    """List jobs."""
    with _client() as c:
        _worker_health_banner(c)
        params: dict[str, str | bool] = {}
        if state:
            params["state_filter"] = state
        if project:
            params["project"] = project
        if warnings:
            params["warnings_only"] = True
        r = c.get("/jobs", params=params)
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
                        "✓"
                        if st == "completed"
                        else (
                            "✗" if st in {"failed", "cancelled", "preempted", "orphaned"} else "⧖"
                        )
                    )
                    parts.append(f"{d}{mark}")
                typer.echo(f"  deps: {' '.join(parts)}")
            if j.get("warning"):
                typer.secho(f"  ⚠ {j['warning']}", fg="yellow")
            for line in _eta_lines(j):
                typer.echo(line)


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
    lines.extend(_eta_lines(j))
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
    with JobdClient(base_url=BASE) as c:
        job = c.cancel(job_id)
        typer.echo(json.dumps(job, default=str))


@app.command()
def preempt(job_id: int):
    """Signal a running/assigned preemptible job to terminate (SIGTERM with
    grace, then SIGKILL). Final state will be 'preempted'. Refused if the job
    is not running/assigned or not preemptible."""
    with _client() as c:
        try:
            job = c.preempt(job_id)
        except BrokerRefusal as e:
            typer.secho(f"refused ({e.status_code}): {e.detail}", fg="red", err=True)
            raise typer.Exit(code=1)
        typer.echo(json.dumps(job, default=str))


@app.command(name="preempt-blockers")
def preempt_blockers(
    job_id: int,
    force: bool = typer.Option(
        False,
        "--force",
        help="Drop the priority guard — preempt blockers at equal-or-higher priority too.",
    ),
):
    """Manual escalation: find a preemptible blocker for queued JOB_ID on
    its eligible workers and signal it now (skips the sweeper's 5-min
    queue-age and runtime guards). Use this to fire auto-preempt
    immediately instead of waiting for the next sweep."""
    with _client() as c:
        try:
            r = c.post(f"/jobs/{job_id}/preempt-blockers", params={"force": force})
        except BrokerRefusal as e:
            typer.secho(f"refused ({e.status_code}): {e.detail}", fg="red", err=True)
            raise typer.Exit(code=1)
        body = r.json()
        if body.get("signaled") is None:
            typer.secho(f"no blocker signaled: {body.get('reason')}", fg="yellow")
            raise typer.Exit(code=2)
        typer.echo(json.dumps(body, default=str))


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
def workers():
    """List registered workers and their health status."""
    with JobdClient(base_url=BASE) as c:
        data = c.workers()
        typer.echo(json.dumps(data, default=str))


@app.command(name="gpu-holders")
def gpu_holders(
    only_unknown: bool = typer.Option(
        False,
        "--only-unknown",
        help="Audit 2026-05-18 spec-review (S5 fix): filter to PIDs the "
        "broker does NOT recognize as currently-running jobs (kill-or-STOP "
        "decision surface).",
    ),
):
    """Probe the broker host for GPU-holding processes (NVML ∪ fuser).

    Audit 2026-05-18 (runtime-zombies S5): NVML returns [N/A] for some
    held-GPU processes; `fuser -v /dev/nvidia*` catches what NVML
    misses. Probes the broker host, which is rarely the GPU host —
    primarily useful when the broker and a worker share a host, or for
    operators ssh'd to the GPU host running a local broker.
    """
    with JobdClient(base_url=BASE) as c:
        r = c.get("/gpu-holders")
        rows = r.json()
        if only_unknown:
            rows = [h for h in rows if not h.get("known", False)]
        typer.echo(json.dumps(rows, default=str))


@app.command()
def ping(
    timeout: float = typer.Option(5.0, "--timeout", "-t", help="HTTP timeout in seconds"),
    json_output: bool = typer.Option(False, "--json", help="emit JSON instead of human text"),
):
    """Probe the broker /health endpoint and report reachability + latency.

    Diagnostic for connectivity questions (off-LAN, post-restart, "is it me
    or the broker?"). Exits 0 if /health returned status=ok, 2 otherwise.
    """
    start = time.monotonic()
    error: str | None = None
    payload: dict[str, Any] | None = None

    try:
        with JobdClient(base_url=BASE, timeout=(timeout, timeout)) as c:
            r = c.get("/health")
            payload = r.json()
    except BrokerUnreachable as e:
        error = str(e)
    except (BrokerServerError, BrokerRefusal) as e:
        error = f"broker error: {e}"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    elapsed_ms = int((time.monotonic() - start) * 1000)
    healthy = error is None and (payload or {}).get("status") == "ok"

    if json_output:
        result = {
            "broker": BASE,
            "reachable": error is None,
            "healthy": healthy,
            "latency_ms": elapsed_ms,
            "version": (payload or {}).get("version"),
            "error": error,
        }
        typer.echo(json.dumps(result))
    else:
        typer.echo(f"broker:  {BASE}")
        if healthy:
            typer.echo("health:  ok")
            typer.echo(f"version: {(payload or {}).get('version', '?')}")
            typer.echo(f"latency: {elapsed_ms}ms")
        else:
            typer.secho("health:  unreachable", fg="red", err=True)
            typer.echo(f"latency: {elapsed_ms}ms")
            typer.echo(f"error:   {error}", err=True)

    if not healthy:
        raise typer.Exit(code=2)


@app.command(name="delete-worker")
def delete_worker(host: str):
    """Remove a worker from the broker registry. The worker must be offline
    (stop the worker process first, or wait for the sweeper to mark it offline)."""
    with _client() as c:
        try:
            result = c.delete_worker(host)
        except BrokerRefusal as e:
            typer.secho(f"refused ({e.status_code}): {e.detail}", fg="red", err=True)
            raise typer.Exit(code=1)
        typer.echo(json.dumps(result, default=str))


@app.command()
def classify(cmd: str):
    with _client() as c:
        r = c.post("/classify", json={"cmd": cmd})
        r.raise_for_status()
        typer.echo(json.dumps(r.json(), indent=2))


projects_app = typer.Typer(help="Manage project priorities.")
app.add_typer(projects_app, name="projects")


def _entry_priority(v) -> int:
    """Server now returns ProjectEntry-shaped dicts {priority, defaults}.
    Handle both shapes for forward/backward compatibility."""
    if isinstance(v, dict):
        return int(v.get("priority", 0))
    return int(v)


@projects_app.command("list")
def projects_list():
    with _client() as c:
        r = c.get("/projects")
        items = r.json()
        for name, entry in sorted(items.items(), key=lambda kv: -_entry_priority(kv[1])):
            pri = _entry_priority(entry)
            defaults = entry.get("defaults") if isinstance(entry, dict) else None
            extras = ""
            if defaults:
                bits = []
                if defaults.get("host_pin"):
                    bits.append(f"host={defaults['host_pin']}")
                if defaults.get("max_wall_s") is not None:
                    bits.append(f"max_wall={defaults['max_wall_s']}s")
                if defaults.get("idle_timeout_s") is not None:
                    bits.append(f"idle={defaults['idle_timeout_s']}s")
                if defaults.get("checkpoint_grace_s") is not None:
                    bits.append(f"ckpt_grace={defaults['checkpoint_grace_s']}s")
                if defaults.get("preemptible") is not None:
                    bits.append(f"preemptible={str(defaults['preemptible']).lower()}")
                req = defaults.get("requires")
                if req:
                    if req.get("gpu"):
                        bits.append("gpu")
                    if req.get("needs"):
                        bits.append(f"needs={list(req['needs'])}")
                if bits:
                    extras = "  defaults: " + " ".join(bits)
            typer.echo(f"{name:>30}  {pri:>3}{extras}")


@projects_app.command("set")
def projects_set(name: str, priority: int):
    with _client() as c:
        r = c.post(f"/projects/{name}", json={"priority": priority})
        r.raise_for_status()
        typer.echo(f"{name} -> {_entry_priority(r.json()[name])}")


@projects_app.command("nudge")
def projects_nudge(name: str, delta: int):
    with _client() as c:
        r = c.post(f"/projects/{name}/nudge", json={"delta": delta})
        r.raise_for_status()
        typer.echo(f"{name} -> {_entry_priority(r.json()[name])}")


# ---------------------------------------------------------------------------
# graph: cross-project DAG view
# ---------------------------------------------------------------------------

_SINCE_RE = re.compile(r"^(\d+)([hdw])$")
_GRAPH_GLYPHS: dict[str, str] = {
    "completed": "✓",
    "running": "→",
    "queued": "⧖",
    "blocked": "⧖",
    "assigned": "→",
    "failed": "✗",
    "cancelled": "⊘",
    "preempted": "⊘",
    "orphaned": "⊘",
}
_GRAPH_DOT_COLORS: dict[str, str] = {
    "completed": "green",
    "running": "gold",
    "assigned": "gold",
    "queued": "gray",
    "blocked": "gray",
    "failed": "red",
    "cancelled": "magenta",
    "preempted": "magenta",
    "orphaned": "red",
}


def _parse_since(s: str) -> timedelta:
    m = _SINCE_RE.match(s.strip())
    if not m:
        raise typer.BadParameter(f"--since must look like Nh|Nd|Nw (got {s!r})")
    n = int(m.group(1))
    if n <= 0:
        raise typer.BadParameter("--since must be positive")
    unit = m.group(2)
    return {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]


def _filter_recent(jobs: list[dict], since: timedelta) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - since
    out: list[dict] = []
    for j in jobs:
        ts = j.get("submitted_at")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt >= cutoff:
            out.append(j)
    return out


def _orphan_ids(jobs: list[dict]) -> set[int]:
    """Children of any failed/cancelled/preempted parent visible in `jobs`."""
    by_id = {j["id"]: j for j in jobs}
    orphans: set[int] = set()
    for j in jobs:
        for parent_id in j.get("depends_on") or []:
            parent = by_id.get(parent_id)
            if parent is None:
                continue
            if parent["state"] in TERMINAL_FAIL_STATES:
                orphans.add(j["id"])
                break
    return orphans


def _build_adjacency(
    jobs: list[dict],
) -> tuple[dict[int, list[int]], dict[int, dict], list[int]]:
    """Return (children_by_parent, jobs_by_id, root_ids).

    Roots: jobs with no `depends_on`, OR whose `depends_on` parents are all
    outside the visible window. Sorted by id ascending for stable output.
    """
    by_id = {j["id"]: j for j in jobs}
    children: dict[int, list[int]] = {jid: [] for jid in by_id}
    roots: list[int] = []
    for j in jobs:
        deps = [d for d in (j.get("depends_on") or []) if d in by_id]
        if not deps:
            roots.append(j["id"])
        for d in deps:
            children[d].append(j["id"])
    for kids in children.values():
        kids.sort()
    roots.sort()
    return children, by_id, roots


def _job_one_liner(j: dict, *, orphaned: bool) -> str:
    glyph = _GRAPH_GLYPHS.get(j["state"], "?")
    cmd = " ".join(j["cmd"])
    if len(cmd) > 60:
        cmd = cmd[:57] + "..."
    state_label = "orphaned" if orphaned else j["state"]
    return f"[{j['id']} {glyph} {state_label} {j['project']}] {cmd}"


def _render_ascii(jobs: list[dict], orphan_ids: set[int]) -> str:
    if not jobs:
        return "(no jobs in window)\n"
    children, by_id, roots = _build_adjacency(jobs)
    lines: list[str] = []
    rendered_full: set[int] = set()

    def visit(jid: int, depth: int) -> None:
        indent = "  " * depth
        j = by_id[jid]
        is_orphan = jid in orphan_ids
        if jid in rendered_full:
            lines.append(f"{indent}…{jid} (fan-in)")
            return
        rendered_full.add(jid)
        lines.append(indent + _job_one_liner(j, orphaned=is_orphan))
        for child in children.get(jid, []):
            visit(child, depth + 1)

    for r in roots:
        visit(r, 0)
    # Any jobs unreachable from a visible root (cycle / dangling) — render flat
    for j in jobs:
        if j["id"] not in rendered_full:
            lines.append(_job_one_liner(j, orphaned=j["id"] in orphan_ids))
    return "\n".join(lines) + "\n"


def _render_dot(jobs: list[dict], orphan_ids: set[int]) -> str:
    lines = ["digraph jobs {", '  node [shape=box, style="rounded,filled"];']
    for j in jobs:
        color = "red" if j["id"] in orphan_ids else _GRAPH_DOT_COLORS.get(j["state"], "white")
        cmd = " ".join(j["cmd"])
        if len(cmd) > 40:
            cmd = cmd[:37] + "..."
        label = f"{j['id']}\\n{j['project']}\\n{j['state']}\\n{cmd}"
        label = label.replace('"', "'")
        lines.append(f'  {j["id"]} [label="{label}", color={color}];')
    by_id = {j["id"] for j in jobs}
    for j in jobs:
        for parent_id in j.get("depends_on") or []:
            if parent_id in by_id:
                lines.append(f"  {parent_id} -> {j['id']};")
    lines.append("}")
    return "\n".join(lines) + "\n"


@app.command()
def graph(
    project: str | None = typer.Option(None, help="Filter to a single project."),
    since: str = typer.Option(
        "24h", help="Only show jobs submitted within Nh|Nd|Nw (default 24h)."
    ),
    fmt: str = typer.Option("ascii", "--format", help="ascii (default) or dot (pipe to graphviz)."),
    state: str | None = typer.Option(None, help="Filter by job state."),
):
    """Render a cross-project dependency DAG of recent jobs."""
    if fmt not in {"ascii", "dot"}:
        raise typer.BadParameter("--format must be 'ascii' or 'dot'")
    window = _parse_since(since)
    with _client() as c:
        params: dict[str, str | bool] = {}
        if project:
            params["project"] = project
        if state:
            params["state_filter"] = state
        r = c.get("/jobs", params=params)
        r.raise_for_status()
        jobs = r.json()
    jobs = _filter_recent(jobs, window)
    orphans = _orphan_ids(jobs)
    out = _render_dot(jobs, orphans) if fmt == "dot" else _render_ascii(jobs, orphans)
    typer.echo(out, nl=False)


@app.command()
def audit(
    since: str = typer.Option("24h", help="Window: Nh|Nd|Nw or ISO timestamp."),
    project: str | None = typer.Option(None, help="Filter to a single project."),
    event: str | None = typer.Option(None, help="Filter to a single event name."),
    job_id: int | None = typer.Option(None, "--job-id", help="Filter to a single job."),
    source: str | None = typer.Option(None, help="Filter by source (broker|worker|mcp|hook)."),
    limit: int = typer.Option(1000, help="Max rows (default 1000)."),
    fmt: str = typer.Option("ascii", "--format", help="ascii (default) or json (NDJSON)."),
):
    """Query the broker's event-stream audit log."""
    if fmt not in {"ascii", "json"}:
        raise typer.BadParameter("--format must be 'ascii' or 'json'")
    params: dict[str, Any] = {"since": since, "limit": limit}
    if project:
        params["project"] = project
    if event:
        params["event"] = event
    if job_id is not None:
        params["job_id"] = job_id
    if source:
        params["source"] = source
    with _client() as c:
        r = c.get("/events", params=params)
        r.raise_for_status()
        rows = r.json()
    if fmt == "json":
        for row in rows:
            typer.echo(json.dumps(row))
        return
    for row in rows:
        ts_raw = row.get("ts", "") or ""
        ts_short = ts_raw[:19].replace("T", " ") if isinstance(ts_raw, str) else ""
        job_str = f"#{row['job_id']}" if row.get("job_id") is not None else "-"
        proj = row.get("project") or "-"
        event_name = row.get("event", "") or ""
        payload = row.get("payload") or {}
        payload_str = " ".join(f"{k}={v}" for k, v in payload.items())[:80]
        typer.echo(f"{ts_short}  {event_name:<22}  {job_str:>6}  {proj:<14}  {payload_str}")


if __name__ == "__main__":
    app()
