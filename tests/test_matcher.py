"""Tests for the matcher — pure logic, no DB."""

from datetime import datetime, timedelta, UTC
from dataclasses import dataclass

from jobd.matcher import (
    WorkerSnapshot,
    fits_on_worker,
    pick_next_job,
)
from jobd.models import JobRequires


@dataclass
class FakeJob:
    id: int
    priority: int
    submitted_at: datetime
    host_pin: str
    vram_gb: float
    ram_gb: float
    cpus: int
    requires: JobRequires | None = None


def worker(host="desktop", free_vram=30.0, unreg_vram=0.0, free_ram=28.0, cpus=10):
    return WorkerSnapshot(
        host=host,
        host_aliases=["any", "any-gpu"] if free_vram > 0 else ["any"],
        free_vram_gb=free_vram,
        unregistered_vram_gb=unreg_vram,
        free_ram_gb=free_ram,
        idle_cpus=cpus,
        arch="x86_64",
        os="linux",
        gpu=free_vram > 0,
        tags=[],
    )


def test_fits_on_worker_happy():
    w = worker()
    j = FakeJob(1, 80, datetime.now(UTC), "desktop", vram_gb=20, ram_gb=16, cpus=6)
    assert fits_on_worker(j, w) is True


def test_fits_on_worker_vram_too_small():
    w = worker(free_vram=10.0)
    j = FakeJob(1, 80, datetime.now(UTC), "desktop", vram_gb=20, ram_gb=16, cpus=6)
    assert fits_on_worker(j, w) is False


def test_fits_on_worker_unregistered_subtracted():
    w = worker(free_vram=20.0, unreg_vram=15.0)
    j = FakeJob(1, 80, datetime.now(UTC), "desktop", vram_gb=10, ram_gb=16, cpus=6)
    # effective = 20 - 15 - safety(1) = 4, need 10 → fails
    assert fits_on_worker(j, w) is False


def test_fits_on_worker_host_pin_mismatch():
    w = worker(host="laptop")
    j = FakeJob(1, 80, datetime.now(UTC), "desktop", vram_gb=20, ram_gb=16, cpus=6)
    assert fits_on_worker(j, w) is False


def test_fits_on_worker_host_pin_any():
    w = worker(host="laptop")
    j = FakeJob(1, 80, datetime.now(UTC), "any", vram_gb=4, ram_gb=4, cpus=2)
    assert fits_on_worker(j, w) is True


def test_pick_next_priority_wins():
    w = worker()
    t = datetime.now(UTC)
    jobs = [
        FakeJob(1, 55, t, "desktop", 4, 4, 2),
        FakeJob(2, 80, t + timedelta(seconds=1), "desktop", 4, 4, 2),
        FakeJob(3, 70, t - timedelta(seconds=5), "desktop", 4, 4, 2),
    ]
    pick = pick_next_job(jobs, w)
    assert pick.id == 2


def test_pick_next_fifo_tiebreak():
    w = worker()
    t = datetime.now(UTC)
    jobs = [
        FakeJob(1, 80, t + timedelta(seconds=5), "desktop", 4, 4, 2),  # newer, yields
        FakeJob(2, 80, t, "desktop", 4, 4, 2),  # older, wins
    ]
    pick = pick_next_job(jobs, w)
    assert pick.id == 2


def test_pick_next_skips_nonfitting():
    w = worker(free_vram=5.0)
    t = datetime.now(UTC)
    jobs = [
        FakeJob(1, 90, t, "desktop", 20, 4, 2),  # doesn't fit, highest priority
        FakeJob(2, 50, t, "desktop", 2, 2, 1),  # fits, lower priority
    ]
    pick = pick_next_job(jobs, w)
    assert pick.id == 2


def test_pick_next_none_when_no_fit():
    w = worker(free_vram=1.0, free_ram=1.0, cpus=1)
    t = datetime.now(UTC)
    jobs = [FakeJob(1, 90, t, "desktop", 20, 20, 8)]
    assert pick_next_job(jobs, w) is None


@dataclass
class FakeJob2:
    id: int
    priority: int
    submitted_at: datetime
    host_pin: str
    vram_gb: float
    ram_gb: float
    cpus: int
    requires: JobRequires | None


def worker2(
    host="desktop",
    arch="x86_64",
    os_="linux",
    gpu=True,
    tags=None,
    free_vram=30.0,
    unreg_vram=0.0,
    free_ram=28.0,
    cpus=10,
):
    return WorkerSnapshot(
        host=host,
        host_aliases=["any", "any-gpu"] if gpu else ["any"],
        free_vram_gb=free_vram,
        unregistered_vram_gb=unreg_vram,
        free_ram_gb=free_ram,
        idle_cpus=cpus,
        arch=arch,
        os=os_,
        gpu=gpu,
        tags=list(tags or []),
    )


def _fake(host_pin="any", requires=None, vram=0.0, ram=0.0, cpus=1):
    return FakeJob2(1, 80, datetime.now(UTC), host_pin, vram, ram, cpus, requires)


def test_selector_needs_all_match():
    w = worker2(tags=["R", "python3"])
    j = _fake(requires=JobRequires(needs=["R"]))
    assert fits_on_worker(j, w) is True


def test_selector_needs_missing_tag_rejects():
    w = worker2(tags=["python3"])
    j = _fake(requires=JobRequires(needs=["R"]))
    assert fits_on_worker(j, w) is False


def test_selector_arch_mismatch_rejects():
    w = worker2(arch="x86_64")
    j = _fake(requires=JobRequires(arch="arm64"))
    assert fits_on_worker(j, w) is False


def test_selector_arch_any_passes():
    w = worker2(arch="arm64")
    j = _fake(requires=JobRequires(arch="any"))
    assert fits_on_worker(j, w) is True


def test_selector_gpu_required_but_worker_has_none():
    w = worker2(gpu=False, free_vram=0.0)
    j = _fake(requires=JobRequires(gpu=True))
    assert fits_on_worker(j, w) is False


def test_selector_gpu_forbidden_but_worker_has_one():
    w = worker2(gpu=True)
    j = _fake(requires=JobRequires(gpu=False))
    assert fits_on_worker(j, w) is False


def test_selector_none_requires_matches_any_worker():
    w = worker2(arch="arm64", gpu=False, tags=[])
    j = _fake(requires=None)
    assert fits_on_worker(j, w) is True


def test_selector_os_linux_matches_linux_worker():
    w = worker2(os_="linux")
    j = _fake(requires=JobRequires(os="linux"))
    assert fits_on_worker(j, w) is True


def test_selector_os_any_matches_all():
    w = worker2(os_="darwin")
    j = _fake(requires=JobRequires(os="any"))
    assert fits_on_worker(j, w) is True
