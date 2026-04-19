"""FastAPI application factory for jobd.

All endpoints in this file for now; split into submodules when it grows past
~500 lines or gets distinct subsystems.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, UTC
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sse_starlette.sse import EventSourceResponse

from jobd.config import (
    load_projects,
    load_profiles,
    load_classifier_rules,
    resolve_priority,
    resolve_profile,
)
from jobd.db import Job, Worker, init_db, migrate
from jobd.models import (
    JobSubmit,
    JobInfo,
    JobState,
    ClassifyRequest,
    ClassifyResult,
    WorkerHeartbeat,
    NextJobQuery,
)

log = logging.getLogger("jobd")

TERMINAL_STATES = {
    JobState.COMPLETED,
    JobState.FAILED,
    JobState.CANCELLED,
    JobState.PREEMPTED,
    JobState.ORPHANED,
}


def build_app(
    db_url: str,
    projects_path: Path | str,
    profiles_path: Path | str,
    classifier_path: Path | str,
    logs_path: Path | str | None = None,
) -> FastAPI:
    app = FastAPI(title="jobd", version="0.1.0")

    engine = create_engine(db_url, future=True)
    init_db(engine)
    migrate(engine)
    SessionLocal = sessionmaker(engine, expire_on_commit=False)

    logs_dir = Path(logs_path) if logs_path else Path(os.environ.get("JOBD_LOGS_DIR", "./logs"))
    logs_dir.mkdir(parents=True, exist_ok=True)

    state = {
        "projects": load_projects(projects_path),
        "profiles": load_profiles(profiles_path),
        "classifier": load_classifier_rules(classifier_path),
        "paths": {
            "projects": Path(projects_path),
            "profiles": Path(profiles_path),
            "classifier": Path(classifier_path),
        },
        "logs_dir": logs_dir,
    }
    app.state.shared = state
    app.state.SessionLocal = SessionLocal

    @app.get("/health")
    def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.post("/submit", response_model=JobInfo)
    def submit(req: JobSubmit):
        profile_spec = None
        if req.profile:
            profile_spec = resolve_profile(state["profiles"], req.profile)
            if profile_spec is None:
                raise HTTPException(status_code=404, detail=f"unknown profile: {req.profile}")

        priority = resolve_priority(state["projects"], req.project, req.priority_delta)
        host_pin = req.host_pin
        if host_pin == "any" and profile_spec and profile_spec.host_hint:
            host_pin = profile_spec.host_hint

        vram_gb = profile_spec.vram_gb if profile_spec else 0
        ram_gb = profile_spec.ram_gb if profile_spec else 0
        cpus = profile_spec.cpus if profile_spec else 1
        preemptible = req.preemptible or (profile_spec.preemptible if profile_spec else False)

        requires = req.requires
        if requires is None and profile_spec and profile_spec.requires:
            requires = profile_spec.requires
        requires_json = requires.model_dump_json() if requires is not None else "{}"

        with SessionLocal() as session:
            job = Job(
                project=req.project,
                profile=req.profile,
                host_pin=host_pin,
                priority=priority,
                state=JobState.QUEUED,
                cmd_json=json.dumps(req.cmd),
                cwd=req.cwd,
                env_json=json.dumps(req.env),
                preemptible=preemptible,
                vram_gb=vram_gb,
                ram_gb=ram_gb,
                cpus=cpus,
                session_id=req.session_id,
                submitted_at=datetime.now(UTC),
                requires_json=requires_json,
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            return _to_info(job)

    @app.get("/jobs", response_model=list[JobInfo])
    def list_jobs(state_filter: str | None = None, project: str | None = None):
        with SessionLocal() as session:
            stmt = select(Job).order_by(Job.id.desc())
            if state_filter:
                stmt = stmt.where(Job.state == state_filter)
            if project:
                stmt = stmt.where(Job.project == project)
            jobs = session.execute(stmt).scalars().all()
            return [_to_info(j) for j in jobs]

    @app.get("/jobs/{job_id}", response_model=JobInfo)
    def get_job(job_id: int):
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            return _to_info(job)

    @app.post("/jobs/{job_id}/log")
    async def append_log(job_id: int, request: Request):
        body = await request.body()
        log_file = logs_dir / f"{job_id}.log"
        with log_file.open("ab") as f:
            f.write(body)
        return {"bytes": len(body)}

    @app.post("/jobs/{job_id}/complete", response_model=JobInfo)
    def complete_job(job_id: int, payload: dict):
        exit_code = payload.get("exit_code")
        final_state = payload.get("final_state", "completed")
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            job.state = final_state
            job.exit_code = exit_code
            job.finished_at = datetime.now(UTC)
            session.commit()
            session.refresh(job)
            return _to_info(job)

    @app.post("/jobs/{job_id}/cancel", response_model=JobInfo)
    def cancel_job(job_id: int):
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            if job.state in (JobState.RUNNING, JobState.ASSIGNED):
                job.signal = "cancel"
            elif job.state == JobState.QUEUED:
                job.state = JobState.CANCELLED
                job.finished_at = datetime.now(UTC)
            session.commit()
            session.refresh(job)
            return _to_info(job)

    @app.get("/jobs/{job_id}/signal")
    def get_signal(job_id: int):
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
            return {"signal": job.signal}

    @app.get("/wait/{job_id}")
    async def wait_job(job_id: int):
        log_file = logs_dir / f"{job_id}.log"

        async def event_generator():
            position = 0
            while True:
                # Read new log bytes since last position
                if log_file.exists():
                    with log_file.open("rb") as f:
                        f.seek(position)
                        chunk = f.read()
                    if chunk:
                        position += len(chunk)
                        yield {
                            "event": "log",
                            "data": chunk.decode("utf-8", errors="replace"),
                        }

                # Check job state
                with SessionLocal() as session:
                    job = session.get(Job, job_id)

                if job is None:
                    yield {"event": "error", "data": "no such job"}
                    return

                if JobState(job.state) in TERMINAL_STATES:
                    yield {
                        "event": "terminal",
                        "data": json.dumps({"state": job.state, "exit_code": job.exit_code}),
                    }
                    return

                await asyncio.sleep(0.5)

        return EventSourceResponse(event_generator())

    @app.post("/classify", response_model=ClassifyResult)
    def classify_endpoint(req: ClassifyRequest) -> ClassifyResult:
        from jobd.classifier import classify as _classify

        return _classify(req.cmd, state["classifier"])

    @app.post("/heartbeat")
    def heartbeat(hb: WorkerHeartbeat):
        with SessionLocal() as session:
            worker = session.execute(
                select(Worker).where(Worker.host == hb.host)
            ).scalar_one_or_none()
            now = datetime.now(UTC)
            if worker is None:
                worker = Worker(host=hb.host, last_heartbeat=now)
                session.add(worker)
            worker.host_aliases_json = json.dumps(hb.host_aliases)
            worker.last_heartbeat = now
            worker.free_vram_gb = hb.free_vram_gb
            worker.unregistered_vram_gb = hb.unregistered_vram_gb
            worker.free_ram_gb = hb.free_ram_gb
            worker.idle_cpus = hb.idle_cpus
            worker.arch = hb.arch
            worker.os = hb.os
            worker.gpu = hb.gpu
            worker.tags_json = json.dumps(hb.tags)
            worker.state = "online"
            session.commit()
            return {"ok": True}

    @app.post("/next-job", response_model=JobInfo | None)
    def next_job(q: NextJobQuery):
        from jobd.matcher import WorkerSnapshot, pick_next_job

        with SessionLocal() as session:
            queued = (
                session.execute(select(Job).where(Job.state == JobState.QUEUED)).scalars().all()
            )
            worker_row = session.execute(
                select(Worker).where(Worker.host == q.host)
            ).scalar_one_or_none()
            aliases: list[str] = ["any", "any-gpu"] if q.free_vram_gb > 0 else ["any"]
            arch = q.arch
            os_ = q.os
            gpu = q.gpu or q.free_vram_gb > 0
            tags: list[str] = list(q.tags)
            if worker_row is not None:
                aliases = json.loads(worker_row.host_aliases_json)
                arch = worker_row.arch
                os_ = worker_row.os
                gpu = worker_row.gpu
                tags = json.loads(worker_row.tags_json)
            w = WorkerSnapshot(
                host=q.host,
                host_aliases=aliases,
                free_vram_gb=q.free_vram_gb,
                unregistered_vram_gb=q.unregistered_vram_gb,
                free_ram_gb=q.free_ram_gb,
                idle_cpus=q.idle_cpus,
                arch=arch,
                os=os_,
                gpu=gpu,
                tags=tags,
            )
            pick = pick_next_job(queued, w)
            if pick is None:
                return None
            # Atomic claim: only one worker can transition queued -> assigned
            result = session.execute(
                Job.__table__.update()
                .where(Job.id == pick.id, Job.state == JobState.QUEUED)
                .values(state=JobState.ASSIGNED, worker=q.host, started_at=datetime.now(UTC))
            )
            session.commit()
            if result.rowcount == 0:
                return None  # lost race; next poll
            session.refresh(pick)
            return _to_info(pick)

    @app.get("/projects")
    def list_projects():
        return state["projects"]

    @app.post("/projects/{name}")
    def set_project_priority(name: str, payload: dict):
        priority = max(0, min(100, int(payload["priority"])))
        state["projects"][name] = priority
        _persist_projects(state)
        return state["projects"]

    @app.post("/projects/{name}/nudge")
    def nudge_project_priority(name: str, payload: dict):
        delta = int(payload["delta"])
        current = state["projects"].get(name, state["projects"].get("_default", 40))
        new_priority = max(0, min(100, current + delta))
        state["projects"][name] = new_priority
        _persist_projects(state)
        return state["projects"]

    @app.post("/reload")
    def reload_config():
        state["projects"] = load_projects(state["paths"]["projects"])
        state["profiles"] = load_profiles(state["paths"]["profiles"])
        state["classifier"] = load_classifier_rules(state["paths"]["classifier"])
        return {"reloaded": True}

    return app


def _persist_projects(state: dict) -> None:
    """Write the in-memory projects dict back to YAML in the canonical shape."""
    data = {
        "projects": {name: {"priority": priority} for name, priority in state["projects"].items()}
    }
    state["paths"]["projects"].write_text(yaml.safe_dump(data, sort_keys=False))


def _to_info(job: Job) -> JobInfo:
    req = None
    if job.requires_json and job.requires_json != "{}":
        from jobd.models import JobRequires

        try:
            req = JobRequires.model_validate_json(job.requires_json)
        except Exception:
            req = None
    return JobInfo(
        id=job.id,
        project=job.project,
        profile=job.profile,
        host_pin=job.host_pin,
        priority=job.priority,
        state=JobState(job.state),
        cmd=json.loads(job.cmd_json),
        cwd=job.cwd,
        preemptible=job.preemptible,
        worker=job.worker,
        submitted_at=job.submitted_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        exit_code=job.exit_code,
        vram_gb=job.vram_gb,
        ram_gb=job.ram_gb,
        cpus=job.cpus,
        requires=req,
        warning=job.warning,
    )
