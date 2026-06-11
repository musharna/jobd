"""Tests for jobd Pydantic models."""

import pytest
from pydantic import ValidationError

from jobd.models import (
    ClassifyRequest,
    JobRequires,
    JobState,
    JobSubmit,
    ResourceReq,
    WorkerHeartbeat,
)


def test_job_submit_minimal():
    req = JobSubmit(cmd=["bash", "train.sh"], cwd="/tmp", project="project-a")
    assert req.profile is None
    assert req.priority_delta == 0
    # preemptible is now bool|None: None means "fall through to project /
    # profile / global" — see docs/projects-yaml.md §6.
    assert req.preemptible is None


def test_job_submit_with_profile():
    req = JobSubmit(
        cmd=["bash", "train.sh"],
        cwd="/tmp",
        project="project-a",
        profile="gpu-heavy",
        priority_delta=10,
        preemptible=True,
    )
    assert req.profile == "gpu-heavy"
    assert req.priority_delta == 10


def test_job_submit_rejects_empty_cmd():
    with pytest.raises(ValidationError):
        JobSubmit(cmd=[], cwd="/tmp", project="project-a")


def test_resource_req_fields():
    r = ResourceReq(vram_gb=28, ram_gb=22, cpus=8)
    assert r.vram_gb == 28


def test_worker_heartbeat():
    hb = WorkerHeartbeat(
        host="desktop-vm",
        free_vram_gb=30.2,
        unregistered_vram_gb=0.5,
        free_ram_gb=25.1,
        idle_cpus=10,
    )
    assert hb.host == "desktop-vm"


def test_classify_request():
    req = ClassifyRequest(cmd="bash train_lora_v5.sh")
    assert req.cmd == "bash train_lora_v5.sh"


def test_job_state_enum():
    assert JobState.QUEUED == "queued"
    assert JobState.RUNNING == "running"
    assert JobState.PREEMPTED == "preempted"


def test_job_requires_defaults():
    r = JobRequires()
    assert r.arch == "any"
    assert r.os == "any"
    assert r.gpu is None
    assert r.needs == []
    assert r.idempotent is False


def test_job_requires_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        JobRequires(needz=["R"])  # typo must fail


def test_job_submit_without_requires():
    j = JobSubmit(cmd=["echo", "hi"], cwd="/tmp", project="p")
    assert j.requires is None


def test_worker_heartbeat_capability_defaults():
    h = WorkerHeartbeat(
        host="x", free_vram_gb=0, unregistered_vram_gb=0, free_ram_gb=8, idle_cpus=4
    )
    assert h.arch == "unknown"
    assert h.os == "unknown"
    assert h.gpu is False
    assert h.tags == []


def test_jobinfo_naive_datetime_serialized_as_utc():
    """Naive datetimes (as round-tripped through the plain DateTime column)
    must serialize with explicit UTC offset, not as bare strings."""
    from datetime import datetime

    from jobd.models import JobInfo

    info = JobInfo(
        id=1,
        project="p",
        profile=None,
        host_pin="any",
        priority=50,
        state=JobState.QUEUED,
        cmd=["echo", "hi"],
        cwd="/tmp",
        preemptible=False,
        worker=None,
        submitted_at=datetime(2026, 4, 21, 14, 3, 13),  # naive → treat as UTC
        started_at=None,
        finished_at=None,
        exit_code=None,
    )
    data = info.model_dump(mode="json")
    assert data["submitted_at"] == "2026-04-21T14:03:13+00:00"
    assert data["started_at"] is None
    assert data["finished_at"] is None


def test_jobinfo_aware_datetime_preserves_offset():
    """Already-aware UTC datetimes serialize with +00:00 intact."""
    from datetime import UTC, datetime

    from jobd.models import JobInfo

    info = JobInfo(
        id=1,
        project="p",
        profile=None,
        host_pin="any",
        priority=50,
        state=JobState.COMPLETED,
        cmd=["echo", "hi"],
        cwd="/tmp",
        preemptible=False,
        worker="desktop",
        submitted_at=datetime(2026, 4, 21, 14, 3, 13, tzinfo=UTC),
        started_at=datetime(2026, 4, 21, 14, 3, 14, tzinfo=UTC),
        finished_at=datetime(2026, 4, 21, 15, 0, 0, tzinfo=UTC),
        exit_code=0,
    )
    data = info.model_dump(mode="json")
    assert data["submitted_at"].endswith("+00:00")
    assert data["started_at"].endswith("+00:00")
    assert data["finished_at"].endswith("+00:00")
