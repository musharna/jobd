"""MCP tool dispatch — calls JobdClient and shapes results per spec §3.

Outbound: `translate.xlate_submit_payload` translates MCP-flat args to the
broker's JobSubmit body. Inbound: `translate.xlate_job_info` /
`wrap_jobs` / `wrap_workers` reshape broker responses to MCP-facing
field names. All translation lives in `translate.py`.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from jobd.client import JobdClient
from jobd.mcp.translate import (
    wrap_jobs,
    wrap_workers,
    xlate_job_info,
    xlate_submit_payload,
)
from jobd.models import TERMINAL_STATES, JobState

POLL_INTERVAL_S = 2.0
MAX_WAIT_S = 270

# Single source of truth in models.py — the old local {completed, failed,
# cancelled} silently dropped preempted/orphaned/scheduling_timeout, so
# wait=true hung until MAX_WAIT_S on any job that reached one of those.
_TERMINAL = TERMINAL_STATES


def _build_submit_payload(args: dict) -> dict:
    """Flatten first-class fields and `extra` into one MCP-flat payload.

    `args` has already been validated against SUBMIT_INPUT (server._dispatch),
    so every key here is one the schema declares and the two key sets are
    disjoint. `xlate_submit_payload` converts the result to the broker
    JobSubmit body at the seam.
    """
    payload = {
        "command": args["command"],
        "project": args["project"],
        "cwd": args["cwd"],
        "submitted_via": "mcp",
    }
    for k in ("needs", "gpu", "host", "dry_run"):
        if k in args:
            payload[k] = args[k]
    for k, v in (args.get("extra") or {}).items():
        payload.setdefault(k, v)
    return payload


def _status(client: JobdClient, job_id: int) -> dict:
    """Fetch + translate JobInfo. Used everywhere status is read."""
    return xlate_job_info(client.status(job_id))


def _wait_for_terminal(client: JobdClient, job_id: int, timeout_s: int) -> tuple[dict, bool]:
    """Poll /status until terminal or timeout. Returns (last_status, timed_out)."""
    deadline = time.monotonic() + timeout_s
    while True:
        info = _status(client, job_id)
        if info["state"] in _TERMINAL:
            return info, False
        if time.monotonic() >= deadline:
            return info, True
        time.sleep(POLL_INTERVAL_S)


def _wait_array_for_terminal(
    client: JobdClient, job_ids: list[int], timeout_s: int
) -> tuple[list[dict], bool]:
    """Poll every array member until all are terminal or a shared deadline elapses.

    The deadline bounds the *aggregate* wait (not per-member), so an N-member
    array can't multiply MAX_WAIT_S. Returns (statuses, timed_out) where
    statuses is in job_ids order; on timeout it carries the last-known state of
    any still-pending member so the caller can report partial progress.
    """
    deadline = time.monotonic() + timeout_s
    done: dict[int, dict] = {}
    pending = list(job_ids)
    while pending:
        still: list[int] = []
        for jid in pending:
            info = _status(client, jid)
            if info["state"] in _TERMINAL:
                done[jid] = info
            else:
                still.append(jid)
        pending = still
        if not pending:
            break
        if time.monotonic() >= deadline:
            for jid in pending:
                done[jid] = _status(client, jid)
            return [done[j] for j in job_ids], True
        time.sleep(POLL_INTERVAL_S)
    return [done[j] for j in job_ids], False


def jobd_submit(client: JobdClient, args: dict) -> dict:
    """Submit a job. Async by default; sync wait supported via wait=True.

    Dry-run (dry_run=true): returns the broker's plan response
    {state: 'dry-run', would_route_to, would_use_worker, validation}
    unchanged — no JobInfo translation because no Job row exists.
    Per dry-run convention 2026-05-18.
    """
    flat = _build_submit_payload(args)
    broker_body = xlate_submit_payload(flat)
    raw = client.submit(broker_body)
    # Dry-run bypasses xlate_job_info (no id/worker/submitted_at fields).
    if isinstance(raw, dict) and raw.get("state") == "dry-run":
        return raw
    # Array submit (count>1 or sweep): broker returns
    # {array_id, count, job_ids, warnings}, not a JobInfo. Without wait, return
    # it as-is; the caller polls members via jobd_status. With wait, poll every
    # member to terminal under one shared deadline and return an aggregate.
    if isinstance(raw, dict) and "job_ids" in raw:
        if not args.get("wait"):
            return raw
        requested = int(args.get("wait_timeout_s", 90))
        timeout_s = min(requested, MAX_WAIT_S)
        clamped = requested > MAX_WAIT_S
        statuses, timed_out_flag = _wait_array_for_terminal(client, raw["job_ids"], timeout_s)
        tally: dict[str, int] = {}
        for s in statuses:
            tally[s["state"]] = tally.get(s["state"], 0) + 1
        out = {
            "array_id": raw["array_id"],
            "count": raw["count"],
            "job_ids": raw["job_ids"],
            "states": tally,
            "all_completed": all(s["state"] == "completed" for s in statuses),
            "members": [
                {
                    "job_id": s["job_id"],
                    "state": s["state"],
                    "exit_code": s.get("exit_code"),
                }
                for s in statuses
            ],
        }
        if raw.get("warnings"):
            out["warnings"] = raw["warnings"]
        if timed_out_flag:
            out["timed_out"] = True
            out["hint"] = "call jobd_status on individual members to keep polling"
        if clamped:
            out["clamped"] = True
        return out
    resp = xlate_job_info(raw)
    base = {
        "job_id": resp["job_id"],
        "state": resp["state"],
        "project": resp.get("project"),
        # null when it equals `project`; set when the broker scheduled the job
        # under a different identity than the agent typed (fold or cwd root).
        "project_label": resp.get("project_label"),
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
        info = _status(client, base["job_id"])
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
    # The wait=true reply is a SUPERSET of the async reply: it used to be
    # rebuilt from scratch, which dropped `warning` and `project_label`, and it
    # returned an 8 KiB tail with no sign the log was longer (audit 2026-09-22
    # M4). The log_* fields mirror jobd_logs' size_bytes/returned_bytes/truncated.
    out = {
        **base,
        "state": info["state"],
        "exit_code": info.get("exit_code"),
        "duration_s": info.get("duration_s"),
        "log_tail": logs.get("tail", ""),
        "log_size_bytes": logs.get("size_bytes"),
        "log_returned_bytes": logs.get("returned_bytes"),
        "log_truncated": logs.get("truncated"),
    }
    if clamped:
        out["clamped"] = True
    return out


def jobd_status(client: JobdClient, args: dict) -> dict:
    job_id = args["job_id"]
    if not args.get("wait"):
        return _status(client, job_id)
    requested = int(args.get("wait_timeout_s", 90))
    timeout_s = min(requested, MAX_WAIT_S)
    info, timed_out = _wait_for_terminal(client, job_id, timeout_s)
    if timed_out:
        info = {**info, "timed_out": True}
    if requested > MAX_WAIT_S:
        info["clamped"] = True
    return info


def jobd_logs(client: JobdClient, args: dict) -> dict:
    """Return broker /output reshape with `log_tail` (not `tail`) for parity
    with `jobd_submit(wait=True)`. Other passthrough fields (size_bytes,
    returned_bytes, truncated) keep their names."""
    raw = client.logs(args["job_id"], tail_bytes=int(args.get("tail_bytes", 8192)))
    out = dict(raw)
    if "tail" in out:
        out["log_tail"] = out.pop("tail")
    return out


def jobd_cancel(client: JobdClient, args: dict) -> dict:
    """Cancel a job; `reason` is recorded on the broker's job_cancelled event.

    signal_sent is read off the broker's OWN cancel reply, not inferred from a
    status read taken before the call (audit 2026-09-22 L11: a job that
    finished between that read and the cancel was reported as signalled). The
    broker's cancel handler either moves a queued job straight to cancelled,
    or stamps signal='cancel' on a running/assigned one in the same
    transaction that produced the reply — so a reply still running/assigned
    means a cancel signal is pending (the worker SIGTERMs on its next poll),
    and anything else means no signal was queued. prior_state is informational.
    """
    job_id = args["job_id"]
    prior = _status(client, job_id)
    after = xlate_job_info(client.cancel(job_id, reason=args.get("reason")))
    signal_sent = "cancel" if after["state"] in ("running", "assigned") else None
    return {
        "job_id": job_id,
        "prior_state": prior["state"],
        "new_state": after["state"],
        "signal_sent": signal_sent,
    }


def jobd_preempt(client: JobdClient, args: dict) -> dict:
    """Preempt a running/assigned preemptible job. The worker SIGTERMs the
    child with grace; final state is 'preempted'. Broker refuses (409) if
    the job is not running/assigned or not preemptible.

    Like cancel, this is async: signal_sent='preempt' acknowledges the
    queued signal, not terminal completion.
    """
    job_id = args["job_id"]
    prior = _status(client, job_id)
    client.preempt(job_id)
    after = _status(client, job_id)
    return {
        "job_id": job_id,
        "prior_state": prior["state"],
        "new_state": after["state"],
        "signal_sent": "preempt",
    }


_LIST_SUMMARY_FIELDS = (
    "job_id",
    "project",
    # The name as typed, when it differs from `project` (the broker's filter
    # matches either, so a row can come back under an identity the agent never
    # typed; without this column the substitution is invisible here).
    "project_label",
    "state",
    "host",
    "exit_code",
    "queued_at",
    "started_at",
    "finished_at",
    # finished_at - started_at, already computed by xlate_job_info; on the row
    # so a run-time question over many jobs needs no per-job jobd_status.
    "duration_s",
)


# Derived, not hand-listed (audit 2026-07-15 L-8): a future non-terminal state
# re-enumerated here would silently vanish from the default view — the exact
# drift class TERMINAL_STATES centralization killed everywhere else.
_LIST_DEFAULT_STATES = tuple(s.value for s in JobState if s.value not in TERMINAL_STATES)
_LIST_LIMIT_DEFAULT = 50
_LIST_LIMIT_MAX = 200
# A finished_at window is filtered here, not by the broker (GET /jobs has no
# time filter; adding one would need a broker upgrade on every deployment).
# The window scan pages through the state/project-filtered set at the broker's
# own page cap, and refuses rather than truncates past _WINDOW_MAX_PAGES: a
# window count that silently covered only part of the history would be wrong
# with no sign of it.
_WINDOW_PAGE = 1000  # the broker's LIST_LIMIT_MAX
_WINDOW_MAX_PAGES = 100
_RELATIVE_WHEN = re.compile(r"^\s*(\d+)\s*([hdw])\s*$")


class ArgumentError(ValueError):
    """A tool argument the schema accepts but whose value is unusable (an
    unparseable timestamp, an empty window). The server reports it as
    invalid_arguments, the caller's to fix, not as an internal error."""

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(message)
        self.hint = hint


def parse_when(name: str, value: str, now: datetime | None = None) -> datetime:
    """'2h' / '3d' / '1w' (that long before now) or ISO-8601 -> aware UTC
    datetime. An ISO value without an offset is read as UTC, the zone every
    broker timestamp is in. Mirrors the broker's own `since` parser."""
    m = _RELATIVE_WHEN.match(value)
    if m:
        n = int(m.group(1))
        if n <= 0:
            raise ArgumentError(f"{name}={value!r}: a relative window must be positive",
                                "Use e.g. 2h, 3d or 1w.")  # fmt: skip
        delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}
        return (now or datetime.now(UTC)) - delta[m.group(2)]
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as e:
        raise ArgumentError(
            f"{name}={value!r} is not a timestamp: {e}",
            "Pass ISO-8601 (2026-10-14T06:00:00Z; no offset means UTC) or a relative "
            "window (2h, 3d, 1w).",
        ) from e
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _finished(row: dict) -> datetime | None:
    v = row.get("finished_at")
    if not v:
        return None
    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _list_finished_window(
    client: JobdClient,
    states: list[str] | None,
    project: str | None,
    after: datetime | None,
    before: datetime | None,
    limit: int,
) -> dict:
    """jobd_list restricted to finished_at in [after, before). Only terminal
    jobs have a finished_at, so an omitted (or empty) `state` means every
    terminal state, and `counts` covers the window exactly, zeroes included."""
    wanted = list(states) if states else sorted(TERMINAL_STATES)
    # One state: let the broker filter. Several: one unfiltered pass is fewer
    # pages than one pass per state.
    state_param = wanted[0] if len(wanted) == 1 else None
    rows: list[dict] = []
    seen: set[int] = set()
    offset = 0
    for _ in range(_WINDOW_MAX_PAGES):
        page, total = client.list_jobs_with_total(
            state=state_param, project=project, limit=_WINDOW_PAGE, offset=offset
        )
        for r in page:
            # A job submitted mid-scan shifts every later row down one place,
            # so the next page can repeat a row; it can never skip one.
            if r.get("id") in seen:
                continue
            seen.add(r.get("id"))
            fin = _finished(r)
            if r.get("state") not in wanted or fin is None:
                continue
            if (after is None or fin >= after) and (before is None or fin < before):
                rows.append(r)
        offset += _WINDOW_PAGE
        if not page or offset >= total:
            break
    else:
        raise ArgumentError(
            f"the finished_at window scan passed {_WINDOW_MAX_PAGES * _WINDOW_PAGE} jobs",
            "Narrow it with `project` or a single `state`.",
        )
    counts = {s: 0 for s in wanted}
    for r in rows:
        counts[r["state"]] += 1
    rows.sort(key=lambda j: j.get("id") or 0, reverse=True)
    wrapped = wrap_jobs(rows)
    jobs = [{k: j.get(k) for k in _LIST_SUMMARY_FIELDS} for j in wrapped["jobs"]][:limit]
    result: dict[str, Any] = {
        "jobs": jobs,
        "counts": counts,
        # Resolved, so a relative window ("3d") says which instants it meant.
        "window": {
            "finished_after": after.isoformat() if after else None,
            "finished_before": before.isoformat() if before else None,
        },
    }
    if len(rows) > len(jobs):
        result["truncated"] = len(rows) - len(jobs)
    return result


def jobd_list(client: JobdClient, args: dict) -> dict:
    # Schema defaults are documentation only — the model may omit the key, so
    # apply them here: absent `state` = the active set (a long-lived broker
    # with retention off holds every job ever run; dumping the full history
    # into the model's context was the audit 2026-07-05 A6 bug). An explicit
    # [] opts into all states.
    states = args.get("state")
    if states is None:
        states = list(_LIST_DEFAULT_STATES)
    try:
        limit = int(args.get("limit", _LIST_LIMIT_DEFAULT))
    except (TypeError, ValueError):
        limit = _LIST_LIMIT_DEFAULT
    limit = max(1, min(limit, _LIST_LIMIT_MAX))

    now = datetime.now(UTC)
    after = before = None
    if args.get("finished_after") is not None:
        after = parse_when("finished_after", args["finished_after"], now)
    if args.get("finished_before") is not None:
        before = parse_when("finished_before", args["finished_before"], now)
    if after is not None or before is not None:
        if after is not None and before is not None and after >= before:
            raise ArgumentError(
                f"empty window: finished_after {after.isoformat()} is not before "
                f"finished_before {before.isoformat()}",
                "finished_after is the start (inclusive), finished_before the end (exclusive).",
            )
        return _list_finished_window(
            client, args.get("state"), args.get("project"), after, before, limit
        )

    # One BOUNDED request per requested state (audit 2026-07-15 L-8): the old
    # implementation forwarded at most one state and pulled the ENTIRE job
    # history to compute counts — the very transfer the broker's pagination
    # was built to end. X-Total-Count keeps the per-state counts exact while
    # the rows stay capped. Explicit []=all is a single bounded request whose
    # counts cover the returned page (noted via `total`).
    project = args.get("project")
    if states:
        rows = []
        counts: dict[str, int] = {}
        for s in states:
            page, total = client.list_jobs_with_total(state=s, project=project, limit=limit)
            rows.extend(page)
            # Record zeroes too (audit 2026-07-24): omitting them left an agent
            # unable to tell "nothing is queued" from "queued was never asked
            # about" — and `counts` is the field agents reason over.
            counts[s] = total or 0
        rows.sort(key=lambda j: j.get("id") or 0, reverse=True)  # newest-first across states
        total_all = sum(counts.values())
    else:
        rows, total_all = client.list_jobs_with_total(project=project, limit=limit)
        counts = dict(Counter(j.get("state") for j in rows))

    wrapped = wrap_jobs(rows)
    jobs = [{k: j.get(k) for k in _LIST_SUMMARY_FIELDS} for j in wrapped["jobs"]]
    result: dict[str, Any] = {"jobs": jobs[:limit], "counts": counts}
    if total_all > len(jobs[:limit]):
        result["truncated"] = total_all - len(jobs[:limit])
    return result


def jobd_workers(client: JobdClient, args: dict) -> dict:
    # Trust WorkerInfo.state — the broker already computes staleness, with an
    # env-tunable threshold (JOBD_STALE_WORKER_THRESHOLD_S). The old local
    # STALE_AFTER_S=60 re-derivation duplicated that tunable's DEFAULT, so an
    # operator who tuned the broker got contradictory verdicts: `job workers`
    # said online while jobd_workers said degraded (audit 2026-07-15 L-7).
    raw = client.workers()
    wrapped = wrap_workers(raw if isinstance(raw, list) else raw.get("workers", []))
    workers = wrapped["workers"]
    if not workers:
        return {"workers": [], "fleet_health": "empty", "warnings": ["no workers registered"]}
    warnings: list[str] = []
    for w in workers:
        state = w.get("state")
        if state and state != "online":
            warnings.append(f"worker {w.get('host', '?')} {state}")
    health = "degraded" if warnings else "healthy"
    return {"workers": workers, "fleet_health": health, "warnings": warnings}


def jobd_worker_delete(client: JobdClient, args: dict) -> dict:
    """Remove a worker from the broker registry. Worker must be offline;
    the broker returns 409 if it is still online (caller stops the process
    or waits for the heartbeat sweeper to mark it offline first)."""
    return client.delete_worker(args["host"])


def jobd_events(client: JobdClient, args: dict) -> dict:
    """The broker's event stream — the only surface that explains WHY, not just what.

    /jobs tells you a job is queued. Only the event stream tells you it has been
    skipped 400 times because no worker advertises `cuda-32gb`.
    """
    return client.events(
        since=args.get("since"),
        project=args.get("project"),
        event=args.get("event"),
        job_id=args.get("job_id"),
        source=args.get("source"),
        limit=int(args.get("limit", 200)),
    )
