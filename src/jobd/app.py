"""FastAPI application factory for jobd.

All endpoints in this file for now; split into submodules when it grows past
~500 lines or gets distinct subsystems.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, UTC
from pathlib import Path

from fastapi import FastAPI, HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from jobd.config import (
    load_projects,
    load_profiles,
    load_classifier_rules,
    resolve_priority,
    resolve_profile,
)
from jobd.db import Base, Job, init_db
from jobd.models import JobSubmit, JobInfo, JobState

log = logging.getLogger("jobd")


def build_app(
    db_url: str,
    projects_path: Path | str,
    profiles_path: Path | str,
    classifier_path: Path | str,
) -> FastAPI:
    app = FastAPI(title="jobd", version="0.1.0")

    engine = create_engine(db_url, future=True)
    init_db(engine)
    SessionLocal = sessionmaker(engine, expire_on_commit=False)

    state = {
        "projects": load_projects(projects_path),
        "profiles": load_profiles(profiles_path),
        "classifier": load_classifier_rules(classifier_path),
        "paths": {
            "projects": Path(projects_path),
            "profiles": Path(profiles_path),
            "classifier": Path(classifier_path),
        },
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

    return app


def _to_info(job: Job) -> JobInfo:
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
    )
