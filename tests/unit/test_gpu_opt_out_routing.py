"""A worker that opts out of GPU work (`gpu: false` in worker.yaml) must not be
handed GPU work through a side door.

`gpu` comes from NVML unless the config overrides it (capabilities.detect), so
a worker with gpu=False and free VRAM exists only when someone switched GPU
work off on purpose -- SCAR18 during its 2026-11 cooling repair: gpu=False,
23.56 GB free, aliases ["any", "any-gpu"]. The `--gpu` selector honored that;
two other routes did not:

  - `--vram-required N` without `--gpu` only compared free VRAM, so the
    opted-out host, with the most free VRAM in the fleet, was the best fit;
  - `--host any-gpu` matched the alias, which NVML presence sets regardless
    of the opt-out.

A VRAM request already implies a GPU elsewhere (adopt.py turns vram_gb > 0
into requires.gpu=True); the matcher now agrees.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from jobd.matcher import WorkerSnapshot, explain_skip, fits_on_worker, pick_next_job
from jobd.models import JobRequires


@dataclass
class _Job:
    id: int
    host_pin: str = "any"
    vram_gb: float = 0.0
    requires: JobRequires | None = None
    priority: int = 50
    submitted_at: datetime = datetime(2026, 11, 9, tzinfo=UTC)
    ram_gb: float = 1.0
    cpus: int = 1


def _worker(*, gpu: bool, host: str) -> WorkerSnapshot:
    # Same NVML view on both: only the configured `gpu` differs.
    return WorkerSnapshot(
        host=host,
        host_aliases=["any", "any-gpu"],
        free_vram_gb=23.56,
        unregistered_vram_gb=0.0,
        free_ram_gb=40.0,
        idle_cpus=20,
        arch="x86_64",
        os="linux",
        gpu=gpu,
        tags=["python3"],
    )


OPTED_OUT = _worker(gpu=False, host="laptop")
GPU_HOST = _worker(gpu=True, host="gt76")


def test_vram_request_without_gpu_flag_skips_an_opted_out_worker():
    job = _Job(1, vram_gb=8.0)
    assert not fits_on_worker(job, OPTED_OUT), (
        "a --vram-required job reached a worker configured gpu: false"
    )
    assert explain_skip([job], OPTED_OUT) == [(1, "no_gpu")]
    # Positive controls: the same job still fits a GPU worker with the same
    # VRAM, and the opted-out worker still takes CPU work.
    assert fits_on_worker(job, GPU_HOST)
    assert fits_on_worker(_Job(2), OPTED_OUT)


def test_cuda_tier_tag_without_gpu_flag_skips_an_opted_out_worker():
    # A cuda-Ngb tag is a VRAM request too (effective_vram_request_gb). The tag
    # is on both workers so only the opt-out can decide it.
    opted_out = _worker(gpu=False, host="laptop")
    opted_out.tags.append("cuda-8gb")
    gpu_host = _worker(gpu=True, host="gt76")
    gpu_host.tags.append("cuda-8gb")
    job = _Job(1, requires=JobRequires(needs=["cuda-8gb"]))
    assert not fits_on_worker(job, opted_out)
    assert fits_on_worker(job, gpu_host)


def test_any_gpu_pin_skips_an_opted_out_worker():
    job = _Job(1, host_pin="any-gpu")
    assert not fits_on_worker(job, OPTED_OUT), (
        "a --host any-gpu job reached a worker configured gpu: false"
    )
    assert explain_skip([job], OPTED_OUT) == [(1, "no_gpu")]
    assert fits_on_worker(job, GPU_HOST)


def test_opted_out_worker_claims_the_cpu_job_behind_a_vram_job():
    vram_job = _Job(1, vram_gb=4.0, priority=90)
    cpu_job = _Job(2, priority=10)
    assert pick_next_job([vram_job, cpu_job], OPTED_OUT) is cpu_job
    assert pick_next_job([vram_job, cpu_job], GPU_HOST) is vram_job
