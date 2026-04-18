"""Tests for the matcher — pure logic, no DB."""
from datetime import datetime, timedelta, UTC
from dataclasses import dataclass

from jobd.matcher import (
    WorkerSnapshot,
    QueuedJob,
    fits_on_worker,
    pick_next_job,
)


@dataclass
class FakeJob:
    id: int
    priority: int
    submitted_at: datetime
    host_pin: str
    vram_gb: float
    ram_gb: float
    cpus: int


def worker(host="desktop", free_vram=30.0, unreg_vram=0.0, free_ram=28.0, cpus=10):
    return WorkerSnapshot(
        host=host,
        host_aliases=["any", "any-gpu"] if free_vram > 0 else ["any"],
        free_vram_gb=free_vram,
        unregistered_vram_gb=unreg_vram,
        free_ram_gb=free_ram,
        idle_cpus=cpus,
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
        FakeJob(2, 80, t, "desktop", 4, 4, 2),                          # older, wins
    ]
    pick = pick_next_job(jobs, w)
    assert pick.id == 2


def test_pick_next_skips_nonfitting():
    w = worker(free_vram=5.0)
    t = datetime.now(UTC)
    jobs = [
        FakeJob(1, 90, t, "desktop", 20, 4, 2),   # doesn't fit, highest priority
        FakeJob(2, 50, t, "desktop", 2, 2, 1),    # fits, lower priority
    ]
    pick = pick_next_job(jobs, w)
    assert pick.id == 2


def test_pick_next_none_when_no_fit():
    w = worker(free_vram=1.0, free_ram=1.0, cpus=1)
    t = datetime.now(UTC)
    jobs = [FakeJob(1, 90, t, "desktop", 20, 20, 8)]
    assert pick_next_job(jobs, w) is None
