"""MCP tool dispatch — calls JobdClient and shapes results per spec §3."""

from __future__ import annotations

import time

from jobd.client import JobdClient

POLL_INTERVAL_S = 2.0
MAX_WAIT_S = 270

_TERMINAL = {"completed", "failed", "cancelled"}


def _build_submit_payload(args: dict) -> dict:
    """Merge first-class fields with extra (extra never overrides explicit fields)."""
    payload = {
        "command": args["command"],
        "project": args["project"],
        "cwd": args["cwd"],
    }
    for k in ("needs", "gpu", "host"):
        if k in args:
            payload[k] = args[k]
    for k, v in (args.get("extra") or {}).items():
        payload.setdefault(k, v)
    return payload


def _wait_for_terminal(client: JobdClient, job_id: int, timeout_s: int) -> tuple[dict, bool]:
    """Poll /status until terminal or timeout. Returns (last_status, timed_out)."""
    deadline = time.monotonic() + timeout_s
    while True:
        info = client.status(job_id)
        if info["state"] in _TERMINAL:
            return info, False
        if time.monotonic() >= deadline:
            return info, True
        time.sleep(POLL_INTERVAL_S)


def jobd_submit(client: JobdClient, args: dict) -> dict:
    """Submit a job. Async by default; sync wait supported via wait=True."""
    payload = _build_submit_payload(args)
    resp = client.submit(payload)
    base = {
        "job_id": resp["job_id"],
        "state": resp["state"],
        "project": resp.get("project"),
        "host_pin": resp.get("host_pin"),
        "queued_at": resp.get("queued_at"),
    }
    if resp.get("warning"):
        base["warning"] = resp["warning"]

    if not args.get("wait"):
        return base

    requested = int(args.get("wait_timeout_s", 90))
    timeout_s = min(requested, MAX_WAIT_S)
    clamped = requested > MAX_WAIT_S

    if base["state"] in _TERMINAL:
        info = client.status(base["job_id"])
    else:
        info, timed_out_flag = _wait_for_terminal(client, base["job_id"], timeout_s)
        if timed_out_flag:
            out = {
                **base,
                "state": info["state"],
                "timed_out": True,
                "hint": "call jobd_status to keep polling",
            }
            if clamped:
                out["clamped"] = True
            return out

    logs = client.logs(base["job_id"], tail_bytes=8192)
    out = {
        "job_id": base["job_id"],
        "state": info["state"],
        "exit_code": info.get("exit_code"),
        "duration_s": info.get("duration_s"),
        "log_tail": logs.get("tail", ""),
    }
    if clamped:
        out["clamped"] = True
    return out


def jobd_status(client: JobdClient, args: dict) -> dict:
    job_id = args["job_id"]
    if not args.get("wait"):
        return client.status(job_id)
    requested = int(args.get("wait_timeout_s", 90))
    timeout_s = min(requested, MAX_WAIT_S)
    info, timed_out = _wait_for_terminal(client, job_id, timeout_s)
    if timed_out:
        info = {**info, "timed_out": True}
    if requested > MAX_WAIT_S:
        info["clamped"] = True
    return info


def jobd_logs(client: JobdClient, args: dict) -> dict:
    return client.logs(args["job_id"], tail_bytes=int(args.get("tail_bytes", 8192)))
