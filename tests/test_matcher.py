"""Tests for the matcher — pure logic, no DB."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from jobd.matcher import (
    GPU_IMPLICIT_FLOOR_GB,
    WorkerSnapshot,
    effective_vram_request_gb,
    fits_on_worker,
    gpu_contention_warning,
    pick_next_job,
    submit_preflight,
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


def test_effective_vram_explicit_wins():
    j = FakeJob(1, 80, datetime.now(UTC), "any", vram_gb=24, ram_gb=8, cpus=2)
    j.requires = JobRequires(gpu=True, needs=["cuda-8gb"])
    # Explicit 24 wins over the cuda-8gb tier-tag fallback (8) and over the
    # 2 GB floor (gpu=True). Caller said "I need 24 GB"; trust them.
    assert effective_vram_request_gb(j) == 24.0


def test_effective_vram_tier_tag_fallback_picks_max():
    j = FakeJob(1, 80, datetime.now(UTC), "any", vram_gb=0, ram_gb=8, cpus=2)
    j.requires = JobRequires(gpu=True, needs=["cuda-8gb", "cuda-16gb", "cuda-12gb"])
    # No explicit vram_gb → max across cuda-Ngb tags wins (16 > 12 > 8 > 2 floor).
    assert effective_vram_request_gb(j) == 16.0


def test_effective_vram_tier_tag_ignores_non_cuda_tags():
    j = FakeJob(1, 80, datetime.now(UTC), "any", vram_gb=0, ram_gb=8, cpus=2)
    j.requires = JobRequires(gpu=True, needs=["python3", "cuda-12gb", "R"])
    assert effective_vram_request_gb(j) == 12.0


def test_effective_vram_implicit_floor_when_only_gpu_true():
    j = FakeJob(1, 80, datetime.now(UTC), "any", vram_gb=0, ram_gb=8, cpus=2)
    j.requires = JobRequires(gpu=True, needs=[])
    # No explicit, no tier tags, but gpu=True → implicit 2 GB floor.
    assert effective_vram_request_gb(j) == GPU_IMPLICIT_FLOOR_GB


def test_effective_vram_zero_for_non_gpu_jobs():
    j = FakeJob(1, 80, datetime.now(UTC), "any", vram_gb=0, ram_gb=8, cpus=2)
    j.requires = None
    assert effective_vram_request_gb(j) == 0.0
    j.requires = JobRequires(gpu=False, needs=["python3"])
    assert effective_vram_request_gb(j) == 0.0


def test_effective_vram_malformed_tier_tag_ignored():
    """`cuda-Xgb` non-int between cuda- and gb falls through; doesn't crash."""
    j = FakeJob(1, 80, datetime.now(UTC), "any", vram_gb=0, ram_gb=8, cpus=2)
    j.requires = JobRequires(gpu=True, needs=["cuda-bigboygb", "cuda-12gb"])
    assert effective_vram_request_gb(j) == 12.0


def test_fits_on_worker_uses_tier_tag_for_vram_gate():
    """Regression for #41d: tier-tag-only jobs gate on the tier number, not 2 GB."""
    w = worker(free_vram=10.0)
    j = FakeJob(1, 80, datetime.now(UTC), "any", vram_gb=0, ram_gb=8, cpus=2)
    j.requires = JobRequires(gpu=True, needs=["cuda-12gb"])
    # effective = 10 - 0 - 1 = 9; need 12 → fails. Pre-#41d this would have
    # passed because the implicit 2 GB floor < 9 GB effective.
    assert fits_on_worker(j, w) is False


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


# --- Implicit GPU floor (Task #42 Fix B) ---
#
# When a job says `requires.gpu == True` but doesn't pin a vram_gb (the common
# case from the CLI/MCP, which never set vram_gb), the matcher used to skip the
# VRAM check entirely. That meant a desktop holding 16+ GB foreign VRAM (idle
# SDXL inference server, the 2026-04-25 incident) would still receive new GPU
# work, which then crawled or OOM'd. We now apply an implicit 2 GB floor for
# GPU-required jobs without an explicit ask, so a fully-loaded GPU worker
# drops out of the matchee set instead of running the job at degraded speed.


def test_implicit_gpu_floor_blocks_fully_loaded_worker():
    # 32 GB GPU, 16 GB held by foreign process → effective = 16-16-1 = -1 GB.
    # Previously: job.vram_gb=0 short-circuited the VRAM check → routed.
    # Now: implicit 2 GB floor for `gpu=True` jobs → does not route.
    w = worker2(free_vram=16.0, unreg_vram=16.0)
    j = _fake(requires=JobRequires(gpu=True))
    assert fits_on_worker(j, w) is False


def test_implicit_gpu_floor_allows_idle_worker():
    # Idle 32 GB GPU, no foreign load → 2 GB floor easily satisfied.
    w = worker2(free_vram=30.0, unreg_vram=0.0)
    j = _fake(requires=JobRequires(gpu=True))
    assert fits_on_worker(j, w) is True


def test_implicit_floor_does_not_apply_when_gpu_not_required():
    # No `gpu=True` selector → no implicit GPU floor, no VRAM check at all
    # (job.vram_gb=0). A CPU-only job doesn't care about foreign GPU load.
    w = worker2(free_vram=16.0, unreg_vram=16.0)
    j = _fake(requires=JobRequires(gpu=None))
    assert fits_on_worker(j, w) is True


def test_explicit_vram_overrides_implicit_floor():
    # If the user asks for 8 GB explicitly, that's the gate — not the 2 GB
    # implicit floor. 16 GB free, 12 GB foreign → effective = 16-12-1 = 3 GB.
    # 8 > 3 → reject (explicit ask wins over implicit floor).
    w = worker2(free_vram=16.0, unreg_vram=12.0)
    j = _fake(requires=JobRequires(gpu=True), vram=8.0)
    assert fits_on_worker(j, w) is False


# --- GPU contention warning (Task #42 Fix A) ---
#
# Surface foreign-process GPU saturation at submit time. Triggers ONLY when
# every eligible GPU worker is saturated (no headroom anywhere). If at least
# one host has VRAM available, no warning — the matcher will route there.
# Quiet when the job doesn't ask for GPU.


def _saturated(host: str) -> WorkerSnapshot:
    # 16 GB total, 16 GB foreign — effective VRAM well below the 2 GB floor.
    return worker2(host=host, free_vram=16.0, unreg_vram=16.0)


def _idle(host: str) -> WorkerSnapshot:
    return worker2(host=host, free_vram=30.0, unreg_vram=0.0)


def test_gpu_contention_warning_silent_when_no_gpu_required():
    snaps = [_saturated("desktop"), _saturated("laptop")]
    msg = gpu_contention_warning(JobRequires(gpu=None), "any", snaps)
    assert msg is None


def test_gpu_contention_warning_silent_when_one_host_has_headroom():
    snaps = [_saturated("desktop"), _idle("laptop")]
    msg = gpu_contention_warning(JobRequires(gpu=True), "any", snaps)
    assert msg is None


def test_gpu_contention_warning_fires_when_all_saturated():
    snaps = [_saturated("desktop"), _saturated("laptop")]
    msg = gpu_contention_warning(JobRequires(gpu=True), "any", snaps)
    assert msg is not None
    assert "desktop" in msg
    assert "laptop" in msg


def test_gpu_contention_warning_silent_when_no_eligible_workers():
    # No CUDA-tagged workers at all → matcher's "soft unmatcheable" path
    # already covers this; we shouldn't double-warn.
    snaps: list[WorkerSnapshot] = []
    msg = gpu_contention_warning(JobRequires(gpu=True), "any", snaps)
    assert msg is None


def test_gpu_contention_warning_respects_host_pin():
    # desktop saturated, laptop idle, but job pinned to desktop → warning
    # because the only eligible host (desktop) is saturated.
    snaps = [_saturated("desktop"), _idle("laptop")]
    msg = gpu_contention_warning(JobRequires(gpu=True), "desktop", snaps)
    assert msg is not None
    assert "desktop" in msg


# ---- submit_preflight (Tier-1 #43) ---------------------------------------


def _w(host: str, *, gpu=True, tags=None, aliases=None, arch="x86_64", os="linux"):
    return WorkerSnapshot(
        host=host,
        host_aliases=aliases or [],
        free_vram_gb=24.0 if gpu else 0.0,
        unregistered_vram_gb=0.0,
        free_ram_gb=28.0,
        idle_cpus=10,
        arch=arch,
        os=os,
        gpu=gpu,
        tags=list(tags or []),
    )


def test_preflight_unknown_host_pin_typo():
    # `--host desktop-vm` typo for `desktop` → no known worker matches.
    snaps = [_w("desktop", aliases=["any-gpu"]), _w("laptop", gpu=False)]
    msg = submit_preflight(None, "desktop-vm", snaps)
    assert msg is not None
    assert "desktop-vm" in msg
    assert "desktop" in msg
    assert "laptop" in msg


def test_preflight_unknown_host_pin_empty_fleet():
    # No registered workers at all → still flags typo (don't silently queue).
    msg = submit_preflight(None, "desktop", [])
    assert msg is not None
    assert "desktop" in msg


def test_preflight_any_host_with_no_workers_is_silent():
    # `--host any` with no workers is a transient empty-fleet, not a typo.
    msg = submit_preflight(None, "any", [])
    assert msg is None


def test_preflight_known_host_alias_resolves():
    # alias matches → not a typo.
    snaps = [_w("desktop", aliases=["desktop-vm"])]
    msg = submit_preflight(None, "desktop-vm", snaps)
    assert msg is None


def test_preflight_unmet_needs_tag():
    # `--needs nonexistent-tag` → flagged at submit, not 60s later.
    snaps = [_w("desktop", tags=["cuda-32gb", "python3"])]
    msg = submit_preflight(JobRequires(needs=["nonexistent-tag"]), "any", snaps)
    assert msg is not None
    assert "nonexistent-tag" in msg


def test_preflight_unmet_gpu_true():
    # No GPU workers → `gpu=True` is unsatisfiable.
    snaps = [_w("cpu-only", gpu=False)]
    msg = submit_preflight(JobRequires(gpu=True), "any", snaps)
    assert msg is not None
    assert "gpu=True" in msg


def test_preflight_unmet_arch():
    # arm64 requested, fleet is x86_64.
    snaps = [_w("desktop")]
    msg = submit_preflight(JobRequires(arch="arm64"), "any", snaps)
    assert msg is not None
    assert "arm64" in msg


def test_preflight_satisfiable_silent():
    # Anything that can route stays silent — no spurious warnings.
    snaps = [_w("desktop", tags=["cuda-32gb"])]
    msg = submit_preflight(JobRequires(gpu=True, needs=["cuda-32gb"]), "any", snaps)
    assert msg is None


def test_preflight_host_pin_known_but_caps_unmet():
    # `--host laptop --needs cuda-32gb` but laptop only has cuda-12gb.
    snaps = [_w("desktop", tags=["cuda-32gb"]), _w("laptop", tags=["cuda-12gb"])]
    msg = submit_preflight(JobRequires(needs=["cuda-32gb"]), "laptop", snaps)
    assert msg is not None
    assert "cuda-32gb" in msg
    assert "laptop" in msg


def test_preflight_offline_worker_still_counts():
    # Snapshot from a worker that's currently offline still represents a
    # capability the fleet has — don't false-positive a typo when the only
    # matching worker is rebooting.
    snaps = [_w("desktop", aliases=["any-gpu"])]  # caller passes ALL workers
    msg = submit_preflight(None, "desktop", snaps)
    assert msg is None


def test_worker_snapshot_carries_mount_roots():
    from jobd.matcher import WorkerSnapshot

    w = WorkerSnapshot(
        host="laptop",
        host_aliases=["any"],
        free_vram_gb=20.0,
        unregistered_vram_gb=0.0,
        free_ram_gb=16.0,
        idle_cpus=8,
        mount_roots=["/home", "/tmp"],
    )
    assert w.mount_roots == ["/home", "/tmp"]


def test_worker_snapshot_mount_roots_defaults_empty():
    from jobd.matcher import WorkerSnapshot

    w = WorkerSnapshot(
        host="x",
        host_aliases=[],
        free_vram_gb=0.0,
        unregistered_vram_gb=0.0,
        free_ram_gb=8.0,
        idle_cpus=4,
    )
    assert w.mount_roots == []


def _wmr(host, roots, aliases=None):
    from jobd.matcher import WorkerSnapshot

    return WorkerSnapshot(
        host=host,
        host_aliases=aliases or ["any"],
        free_vram_gb=10.0,
        unregistered_vram_gb=0.0,
        free_ram_gb=16.0,
        idle_cpus=8,
        mount_roots=roots,
    )


def test_cwd_routability_pinned_host_no_cover_hard_deny():
    from jobd.matcher import cwd_routability

    workers = [_wmr("desktop", ["/home", "/tmp"])]
    out = cwd_routability("/mnt/d/data/x", "desktop", workers)
    assert out is not None and out[0] is True
    assert "/mnt/d/data/x" in out[1]


def test_cwd_routability_any_pin_no_cover_hard_deny():
    # any-pin cwd no advertising worker covers -> hard deny (unroutable anywhere).
    from jobd.matcher import cwd_routability

    workers = [_wmr("gt76", ["/home", "/tmp"]), _wmr("msi", ["/home"])]
    out = cwd_routability("/scratch/run1", "any", workers)
    assert out is not None and out[0] is True
    assert "/scratch/run1" in out[1]


def test_cwd_routability_covered_returns_none():
    from jobd.matcher import cwd_routability

    workers = [_wmr("laptop", ["/home", "/mnt/c"])]
    assert cwd_routability("/home/u/proj", "any", workers) is None


def test_cwd_routability_empty_mount_roots_is_unknown_no_reject():
    from jobd.matcher import cwd_routability

    # worker advertises nothing (old worker) -> unknown, never reject
    workers = [_wmr("legacy", [])]
    assert cwd_routability("/anything/at/all", "any", workers) is None
    assert cwd_routability("/anything/at/all", "legacy", workers) is None


def test_cwd_routability_worktree_under_home_NOT_caught_by_A():
    # Documents the A/B split: every worker advertises /home, so the prefix
    # probe passes a worktree cwd. B (worker-side isdir) is what catches it.
    from jobd.matcher import cwd_routability

    workers = [_wmr("laptop", ["/home"]), _wmr("desktop", ["/home"])]
    cwd = "/home/u/proj/.claude/worktrees/wt"
    assert cwd_routability(cwd, "any", workers) is None
