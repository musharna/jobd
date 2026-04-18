"""Unit tests for the worker — pure functions only. Integration uses a real jobd."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "worker"))

from job_worker import pick_resource_snapshot_mock, compute_unregistered_vram


def test_compute_unregistered_zero_when_all_tracked():
    nvidia_procs = [(1000, 4096), (1001, 2048)]  # (pid, MiB)
    tracked_pids = {1000, 1001}
    assert compute_unregistered_vram(nvidia_procs, tracked_pids) == 0.0


def test_compute_unregistered_counts_untracked():
    nvidia_procs = [(1000, 4096), (9999, 8192)]
    tracked_pids = {1000}
    mib = 8192
    expected_gb = round(mib / 1024.0, 2)
    assert compute_unregistered_vram(nvidia_procs, tracked_pids) == expected_gb
