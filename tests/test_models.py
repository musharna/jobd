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
    req = JobSubmit(cmd=["bash", "train.sh"], cwd="/tmp", project="project-x")
    assert req.profile is None
    assert req.priority_delta == 0
    assert req.preemptible is False


def test_job_submit_with_profile():
    req = JobSubmit(
        cmd=["bash", "train.sh"],
        cwd="/tmp",
        project="project-x",
        profile="gpu-heavy",
        priority_delta=10,
        preemptible=True,
    )
    assert req.profile == "gpu-heavy"
    assert req.priority_delta == 10


def test_job_submit_rejects_empty_cmd():
    with pytest.raises(ValidationError):
        JobSubmit(cmd=[], cwd="/tmp", project="project-x")


def test_resource_req_fields():
    r = ResourceReq(vram_gb=28, ram_gb=22, cpus=8)
    assert r.vram_gb == 28


def test_worker_heartbeat():
    hb = WorkerHeartbeat(
        host="desktop-worker",
        free_vram_gb=30.2,
        unregistered_vram_gb=0.5,
        free_ram_gb=25.1,
        idle_cpus=10,
    )
    assert hb.host == "desktop-worker"


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
