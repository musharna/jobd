"""MCP tool dispatch — calls JobdClient and shapes results per spec §3."""

from __future__ import annotations

from jobd.client import JobdClient


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


def jobd_submit(client: JobdClient, args: dict) -> dict:
    """Submit a job. Async by default; sync wait path added Task 16."""
    payload = _build_submit_payload(args)
    resp = client.submit(payload)
    out = {
        "job_id": resp["job_id"],
        "state": resp["state"],
        "project": resp.get("project"),
        "host_pin": resp.get("host_pin"),
        "queued_at": resp.get("queued_at"),
    }
    if "warning" in resp and resp["warning"]:
        out["warning"] = resp["warning"]
    return out
