"""Job submission as a service: validate, resolve, fan out, persist, cascade, emit.

Extracted from the `/submit` route closure in `jobd.app`, which held ~295 lines of
business logic inside a FastAPI handler. The logic is HTTP-shaped only at its edges
(it raises `HTTPException` for caller errors, because those map exactly onto the 400s
and 404s the API already promises); everything else is callable without a request.

READ THIS BEFORE MOVING ANY NAME OUT OF THIS MODULE'S GLOBALS
-------------------------------------------------------------
Two of the names below are **monkeypatch targets for the tests that guard the two
worst bugs this project has shipped**, and they are patched *through this module's
namespace*:

    _serialization_warning   tests/test_h1_real_race_regression.py
    _FAILED_SIDE_TERMINAL    tests/test_submit_depends_on_toctou.py

They are patched here because that is where `submit_job` resolves them at call time. If
this code moves again, the patch sites must move WITH it. If they don't, the patches
become silent no-ops and both tests keep **passing while testing nothing** — which is
not hypothetical: the 2026-07-12 audit found exactly that, an H-1 fix that was a no-op
in production while its test stayed green (v0.5.13).

A green suite therefore proves nothing about these two. What proves something is
re-injecting each historical bug and confirming the named test *fails*. Both mutations
are pinned as tests in tests/test_submit_service_guards.py — run them, don't trust them.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm.exc import ObjectDeletedError

from jobd.arrays import index_subs, render_cmd, render_env, sweep_member_subs
from jobd.broker.constants import _FAILED_SIDE_TERMINAL
from jobd.broker.context import BrokerState
from jobd.broker.events import _emit_event
from jobd.broker.jobinfo import _build_eta_ctx, _to_info
from jobd.broker.scheduling import _build_snapshots, _serialization_warning
from jobd.broker.state import _cascade_on_parent_terminal, _emit_cascade_cancellations
from jobd.config import resolve_effective_config, resolve_profile
from jobd.db import Job, Worker
from jobd.matcher import (
    cwd_routability,
    eligible_workers,
    gpu_contention_warning,
    submit_preflight,
)
from jobd.models import JobState, JobSubmit


def submit_job(
    req: JobSubmit,
    *,
    session_factory: Callable[[], Any],
    state: BrokerState,
    logs_dir: Path,
    wake_dispatchers: Callable[[], None],
) -> Any:
    """Validate and persist a submission; return the dry-run plan, array summary, or JobInfo.

    Raises HTTPException(400/404) for caller errors, exactly as the route did.
    """
    profile_spec = None
    if req.profile:
        profile_spec = resolve_profile(state["profiles"], req.profile)
        if profile_spec is None:
            raise HTTPException(status_code=404, detail=f"unknown profile: {req.profile}")

    # Shared precedence cascade (CLI > project_default > profile > global,
    # docs/projects-yaml.md §3) resolved once in jobd.config so /submit and
    # /resolve can't drift. /submit reads only `.value`; /resolve surfaces
    # the (value, source) verbatim.
    eff = resolve_effective_config(req, state["projects"], profile_spec)
    priority = eff.priority.value
    host_pin = eff.host_pin.value
    preemptible = eff.preemptible.value
    max_wall_s = eff.max_wall_s.value
    idle_timeout_s = eff.idle_timeout_s.value
    checkpoint_grace_s = eff.checkpoint_grace_s.value
    unknown_project_warning = eff.unknown_project_warning
    requires = eff.requires.value
    requires_json = requires.model_dump_json() if requires is not None else "{}"

    # vram_gb/ram_gb/cpus/fast_path are submit-only (no source label, single
    # consumer, absent from ResolvedConfig): explicit CLI `vram_gb > 0` wins,
    # else the profile spec, else the matcher-friendly floor. See
    # JobSubmit.vram_gb docstring + matcher's `effective_vram_request_gb`.
    vram_gb = req.vram_gb if req.vram_gb > 0 else (profile_spec.vram_gb if profile_spec else 0)
    ram_gb = profile_spec.ram_gb if profile_spec else 0
    cpus = profile_spec.cpus if profile_spec else 1
    if req.fast_path is not None:
        fast_path = req.fast_path
    else:
        fast_path = profile_spec.fast_path if profile_spec else False

    # cwd sanity: Windows-mount paths only exist on the laptop (WSL). If
    # someone submits --cwd /mnt/c/... without pinning the laptop, the
    # worker will fail cd and every process will rc=127. Root cause of the
    # 2026-04-22 project-b storm.
    if req.cwd.startswith("/mnt/c/") and host_pin not in ("laptop", "MSI", "any-laptop"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"cwd {req.cwd!r} is under /mnt/c/ (Windows mount, laptop-only) "
                f"but host_pin={host_pin!r}. Pass --host laptop, or stage data "
                f"under a cross-host path like /tmp or a project-scoped dir."
            ),
        )

    with session_factory() as session:
        for dep_id in req.depends_on:
            parent = session.get(Job, dep_id)
            if parent is None:
                raise HTTPException(
                    status_code=400, detail=f"depends_on refers to missing job: {dep_id}"
                )
            # A default-policy (non-any-exit) child needs the parent to
            # reach COMPLETED. If the parent is ALREADY in a failed-side
            # terminal, it never will — and no future transition fires the
            # cascade to cancel this child, so it would strand in QUEUED
            # forever. Reject at submit instead. (any-exit children are fine:
            # any terminal parent satisfies their dep.)
            if not req.depends_on_any_exit and parent.state in _FAILED_SIDE_TERMINAL:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"depends_on parent {dep_id} is already {parent.state} "
                        "(a failed-side terminal); a default-policy dependent would "
                        "never dispatch. Resubmit the parent, or pass "
                        "depends_on_any_exit to proceed on any terminal state."
                    ),
                )
        now = datetime.now(UTC)
        # Warnings are routing-derived (requires / host_pin / live capacity),
        # so a job array's members share one warning string — the per-member
        # `{i}` substitution only changes the command, not where it routes.
        online = list(
            session.execute(select(Worker).where(Worker.state == "online")).scalars().all()
        )
        snapshots = _build_snapshots(online)
        all_workers = list(session.execute(select(Worker)).scalars().all())
        all_snapshots = _build_snapshots(all_workers)
        ser_warn = _serialization_warning(requires, host_pin, snapshots, session)
        gpu_warn = gpu_contention_warning(requires, host_pin, snapshots)
        preflight_warn = submit_preflight(requires, host_pin, all_snapshots)
        cwd_route = cwd_routability(req.cwd, host_pin, all_snapshots)
        cwd_route_warn: str | None = None
        if cwd_route is not None:
            is_hard, cwd_msg = cwd_route
            if is_hard:
                raise HTTPException(status_code=400, detail=cwd_msg)
            cwd_route_warn = cwd_msg
        warnings = [
            w
            for w in (
                unknown_project_warning,
                preflight_warn,
                cwd_route_warn,
                ser_warn,
                gpu_warn,
            )
            if w is not None
        ]
        warning_text = "; ".join(warnings) if warnings else None

        # Resolve the per-member substitutions up front so dry-run reports
        # the same member count the live submit would create. Three forms,
        # mutually exclusive (sweep+count rejected at the model):
        #   sweep   → cartesian product of named axes, each carrying {i}
        #   count>1 → bare {i} = 0..count-1
        #   neither → a single ordinary job (NULL array columns)
        if req.sweep:
            member_subs = sweep_member_subs([(ax.key, ax.values) for ax in req.sweep])
        elif req.count > 1:
            member_subs = [index_subs(i) for i in range(req.count)]
        else:
            member_subs = None
        is_array = member_subs is not None
        subs_list = member_subs if member_subs is not None else [{}]
        member_count = len(subs_list)

        # Dry-run bail-out:
        # full validation + routing-decision has run above (profile lookup,
        # project defaults, cwd sanity, depends_on existence, preflight,
        # gpu_contention). Return the would-be plan WITHOUT inserting any
        # Job row or emitting `job_submitted`. One plan covers every array
        # member (routing ignores the per-member substitution); array_count
        # tells the caller how many members the live submit would create.
        if req.dry_run:
            eligible = eligible_workers(requires, host_pin, snapshots)
            # would_route_to: hostnames the matcher would currently
            # consider (capability + host_pin match, ignores load). Empty
            # list = unmatcheable right now (preflight_warn explains why).
            would_route_to = [w.host for w in eligible]
            # would_use_worker: pre-load tie-break is matcher-internal; at
            # preview time we surface "any of these candidates" rather
            # than promising a specific host. None when no candidates.
            would_use_worker = eligible[0].host if len(eligible) == 1 else None
            return {
                "state": "dry-run",
                "would_route_to": would_route_to,
                "would_use_worker": would_use_worker,
                "array_count": member_count,
                "validation": {
                    "effective_priority": priority,
                    "effective_host_pin": host_pin,
                    "effective_preemptible": preemptible,
                    "effective_vram_gb": vram_gb,
                    "effective_ram_gb": ram_gb,
                    "effective_cpus": cpus,
                    "effective_max_wall_s": max_wall_s,
                    "effective_idle_timeout_s": idle_timeout_s,
                    "effective_checkpoint_grace_s": checkpoint_grace_s,
                    "effective_scheduling_timeout_s": req.scheduling_timeout_s,
                    "effective_fast_path": fast_path,
                    "effective_requires": (requires.model_dump() if requires is not None else None),
                    "warnings": warnings,
                },
            }

        # Fan out into members using the substitutions resolved above. A
        # non-array submit (member_subs is None) is one ordinary job with
        # NULL array columns and an unchanged single-JobInfo response.
        members: list[Job] = []
        for i, subs in enumerate(subs_list):
            member_cmd = render_cmd(req.cmd, subs) if is_array else req.cmd
            member_env = render_env(req.env, subs) if is_array else req.env
            job = Job(
                project=req.project,
                profile=req.profile,
                host_pin=host_pin,
                priority=priority,
                state=JobState.QUEUED,
                cmd_json=json.dumps(member_cmd),
                cwd=req.cwd,
                env_json=json.dumps(member_env),
                preemptible=preemptible,
                vram_gb=vram_gb,
                ram_gb=ram_gb,
                cpus=cpus,
                session_id=req.session_id,
                submitted_at=now,
                requires_json=requires_json,
                depends_on_json=json.dumps(req.depends_on),
                depends_on_any_exit=req.depends_on_any_exit,
                fast_path=fast_path,
                max_wall_s=max_wall_s,
                idle_timeout_s=idle_timeout_s,
                checkpoint_grace_s=checkpoint_grace_s,
                scheduling_timeout_s=req.scheduling_timeout_s,
                submitted_via=req.submitted_via,
                array_index=i if is_array else None,
                array_size=member_count if is_array else None,
            )
            if warning_text:
                job.warning = warning_text
                job.warning_at = now
            session.add(job)
            members.append(job)

        # Flush to assign ids; the array shares the first member's id so
        # `job status A<id>` resolves without a separate id sequence.
        session.flush()
        member_ids = [j.id for j in members]
        array_id = member_ids[0] if is_array else None
        if is_array:
            for j in members:
                j.array_id = array_id
        session.commit()

        # H-1 (audit 2026-07-10): close the submit depends_on TOCTOU. The
        # failed-side reject above is a point read of parent.state; between
        # it and this insert a parent can reach a failed-side terminal and
        # run its cascade over the then-current QUEUED set — which did NOT
        # yet include these just-committed children. `_deps_satisfied` needs
        # the parent COMPLETED (never happens) and the parent is terminal so
        # no transition re-fires the cascade → a default-policy child would
        # strand QUEUED forever, silently. Re-run the cascade for each
        # distinct parent now that the children are committed and visible: a
        # no-op unless the parent is already failed-side terminal, in which
        # case the fresh child is cancelled exactly as it would have been had
        # it existed when the parent failed. (any-exit children are unaffected
        # — any terminal parent satisfies their dep.)
        toctou_sweeps: list[tuple[int, str, list[tuple[int, str]]]] = []
        if req.depends_on and not req.depends_on_any_exit:
            for dep_id in dict.fromkeys(req.depends_on):
                parent = session.get(Job, dep_id)
                if parent is None:
                    continue
                # The failed-side reject loop above already loaded this
                # parent into the identity map with its pre-race state. With
                # expire_on_commit=False the intervening commit did NOT
                # refresh it, so session.get() returns the SAME cached object
                # and the cascade gate (state.py: `parent.state not in
                # _FAILED_SIDE_TERMINAL`) would read the stale non-terminal
                # state — making this whole sweep a no-op and reopening the
                # exact strand H-1 closes. Refresh from the DB so the
                # re-check sees a parent that reached a failed-side terminal
                # during the submit window (matches the session.refresh(job)
                # idiom every other _cascade_on_parent_terminal caller uses).
                try:
                    session.refresh(parent)
                except ObjectDeletedError:
                    # Parent pruned between commit and re-check; nothing to
                    # cascade from.
                    continue
                swept = _cascade_on_parent_terminal(session, parent)
                if swept:
                    toctou_sweeps.append((parent.id, parent.state, swept))
            if toctou_sweeps:
                session.commit()

        # Emit events from captured scalars (no post-commit ORM reload per
        # member — every member shares project/priority/host_pin/warning).
        for jid in member_ids:
            _emit_event(
                logs_dir,
                "job_submitted",
                source="broker",
                job_id=jid,
                project=req.project,
                priority=priority,
                host_pin=host_pin,
                preemptible=preemptible,
                warning=warning_text,
            )
            if warning_text:
                _emit_event(
                    logs_dir,
                    "submit_warning",
                    source="broker",
                    job_id=jid,
                    project=req.project,
                    warning_text=warning_text,
                )

        # H-1: emit job_cancelled (by='cascade') for any child the TOCTOU
        # sweep cancelled, AFTER the commit above (so a failed commit can't
        # leave events.jsonl claiming a cancel the DB never recorded).
        for _parent_id, _parent_state, _swept in toctou_sweeps:
            _emit_cascade_cancellations(logs_dir, _swept, _parent_id, _parent_state)

        # New queued job(s) — wake any worker long-polling /next-job.
        wake_dispatchers()
        if is_array:
            return {
                "array_id": array_id,
                "count": member_count,
                "job_ids": member_ids,
                "warnings": warnings,
            }
        eta_ctx = _build_eta_ctx(session)
        return _to_info(members[0], eta_ctx)
