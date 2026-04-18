"""Tests for jobd Pydantic models."""
from datetime import datetime

import pytest
from pydantic import ValidationError

from jobd.models import (
    JobState,
    JobSubmit,
    JobInfo,
    ResourceReq,
    WorkerHeartbeat,
    ClassifyRequest,
    ClassifyResult,
)


def test_job_submit_minimal():
    req = JobSubmit(cmd=["bash", "train.sh"], cwd="/tmp", project="orchid-sdxl")
    assert req.profile is None
    assert req.priority_delta == 0
    assert req.preemptible is False


def test_job_submit_with_profile():
    req = JobSubmit(
        cmd=["bash", "train.sh"],
        cwd="/tmp",
        project="orchid-sdxl",
        profile="gpu-heavy",
        priority_delta=10,
        preemptible=True,
    )
    assert req.profile == "gpu-heavy"
    assert req.priority_delta == 10


def test_job_submit_rejects_empty_cmd():
    with pytest.raises(ValidationError):
        JobSubmit(cmd=[], cwd="/tmp", project="orchid-sdxl")


def test_resource_req_fields():
    r = ResourceReq(vram_gb=28, ram_gb=22, cpus=8)
    assert r.vram_gb == 28


def test_worker_heartbeat():
    hb = WorkerHeartbeat(
        host="desktop-wsl",
        free_vram_gb=30.2,
        unregistered_vram_gb=0.5,
        free_ram_gb=25.1,
        idle_cpus=10,
    )
    assert hb.host == "desktop-wsl"


def test_classify_request():
    req = ClassifyRequest(cmd="bash train_lora_v5.sh")
    assert req.cmd == "bash train_lora_v5.sh"


def test_job_state_enum():
    assert JobState.QUEUED == "queued"
    assert JobState.RUNNING == "running"
    assert JobState.PREEMPTED == "preempted"
