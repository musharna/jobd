"""Unit tests for the v1 wall-time estimator (jobd.estimator)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jobd.db import Job, init_db, migrate
from jobd.estimator import (
    CLIP_THRESHOLD,
    HISTORY_WINDOW,
    MIN_SAMPLES,
    ColdStart,
    WallPrediction,
    _quantile,
    cmd_head,
    make_predict_cache,
    predict_wall,
    queue_start_eta,
    remaining_for_running,
)
from jobd.models import JobState

# ---------------- cmd_head ----------------


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ([], ""),
        (["python", "train.py"], "python train.py"),
        (["python", "train.py", "--seed", "0"], "python train.py"),
        (["python3", "/abs/path/run.py"], "python3 run.py"),
        (["/usr/bin/python3", "/abs/path/run.py"], "python3 run.py"),
        (["python", "-m", "torch.distributed.run"], "python -m torch.distributed.run"),
        (["python", "-c", "print(1)"], "python -c"),
        (["Rscript", "pipeline.R"], "Rscript pipeline.R"),
        (["./train.sh"], "train.sh"),
        (["bash", "-c", "echo hi"], "bash -c"),
        (["bash", "build.sh"], "bash build.sh"),
        (["uv", "run", "pytest"], "uv run"),
        (["foo"], "foo"),
        (["/usr/local/bin/foo"], "foo"),
        # Skip --flag --flag value before settling on the script.
        (["python", "--unbuffered", "train.py"], "python train.py"),
    ],
)
def test_cmd_head_cases(cmd, expected):
    assert cmd_head(cmd) == expected


def test_cmd_head_caps_at_80_chars():
    long = ["python", "x" * 200]
    out = cmd_head(long)
    assert len(out) <= 80


# ---------------- quantile math ----------------


def test_quantile_basic():
    assert _quantile([10.0], 0.5) == 10.0
    assert _quantile([10.0, 20.0], 0.5) == 15.0
    # 100 evenly spaced 1..100; p50 is interpolated → 50.5
    vals = [float(i) for i in range(1, 101)]
    assert _quantile(vals, 0.5) == pytest.approx(50.5)
    assert _quantile(vals, 0.9) == pytest.approx(90.1)


def test_quantile_empty():
    assert _quantile([], 0.5) == 0.0


# ---------------- predict_wall ----------------


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/est.db", future=True)
    init_db(engine)
    migrate(engine)
    Session = sessionmaker(engine, future=True)
    with Session() as s:
        yield s


def _add_completed(
    session,
    project: str,
    cmd: list[str],
    wall_s: float,
    *,
    max_wall_s: int | None = None,
    base_id: int | None = None,
):
    """Insert a completed Job row with deterministic timing."""
    started = datetime(2026, 4, 1, tzinfo=UTC) + timedelta(seconds=base_id or 0)
    finished = started + timedelta(seconds=wall_s)
    job = Job(
        id=base_id,
        project=project,
        host_pin="any",
        priority=50,
        state=JobState.COMPLETED.value,
        cmd_json=json.dumps(cmd),
        cwd="/tmp",
        env_json="{}",
        preemptible=False,
        vram_gb=0,
        ram_gb=0,
        cpus=1,
        submitted_at=started,
        started_at=started,
        finished_at=finished,
        max_wall_s=max_wall_s,
    )
    session.add(job)
    session.commit()
    return job


def test_predict_wall_cold_start(session):
    pred = predict_wall(session, "p1", "python train.py")
    assert isinstance(pred, ColdStart)
    assert pred.n_samples == 0
    assert pred.basis == "insufficient-history-N=0"


def test_predict_wall_below_min_samples(session):
    for i in range(MIN_SAMPLES - 1):
        _add_completed(session, "p1", ["python", "t.py"], 100.0, base_id=i + 1)
    pred = predict_wall(session, "p1", "python t.py")
    assert isinstance(pred, ColdStart)
    assert pred.n_samples == MIN_SAMPLES - 1


def test_predict_wall_quantiles_and_basis(session):
    # 10 walls: 60..600s, p50 ≈ 330, p90 ≈ 546
    for i, w in enumerate(range(60, 601, 60)):
        _add_completed(session, "p1", ["python", "t.py"], float(w), base_id=i + 1)
    pred = predict_wall(session, "p1", "python t.py")
    assert isinstance(pred, WallPrediction)
    assert pred.n_samples == 10
    assert pred.p50_s == pytest.approx(330.0)
    assert pred.p90_s == pytest.approx(546.0)
    assert pred.basis == "history-N=10"
    assert pred.clipped is False


def test_predict_wall_clip_detection(session):
    # 5 walls: 4 short, 1 within 95% of max_wall_s=100 (i.e. wall=98)
    for i, w in enumerate([10.0, 12.0, 15.0, 20.0]):
        _add_completed(session, "p1", ["python", "t.py"], w, base_id=i + 1)
    _add_completed(session, "p1", ["python", "t.py"], 98.0, max_wall_s=100, base_id=99)
    pred = predict_wall(session, "p1", "python t.py")
    assert isinstance(pred, WallPrediction)
    assert pred.clipped is True


def test_predict_wall_does_not_mix_projects(session):
    for i in range(5):
        _add_completed(session, "p1", ["python", "t.py"], 100.0, base_id=i + 1)
    for i in range(5):
        _add_completed(session, "p2", ["python", "t.py"], 999.0, base_id=i + 100)
    pred = predict_wall(session, "p1", "python t.py")
    assert isinstance(pred, WallPrediction)
    assert pred.p50_s == pytest.approx(100.0)


def test_predict_wall_does_not_mix_cmd_heads(session):
    for i in range(5):
        _add_completed(session, "p1", ["python", "train.py"], 100.0, base_id=i + 1)
    for i in range(5):
        _add_completed(session, "p1", ["python", "infer.py"], 999.0, base_id=i + 100)
    pred = predict_wall(session, "p1", "python train.py")
    assert isinstance(pred, WallPrediction)
    assert pred.p50_s == pytest.approx(100.0)


def test_predict_wall_history_window_caps_samples(session):
    # Insert 2x HISTORY_WINDOW completions; only the most recent N are sampled.
    n = HISTORY_WINDOW * 2
    for i in range(n):
        wall = 10.0 if i < HISTORY_WINDOW else 1000.0
        _add_completed(session, "p1", ["python", "t.py"], wall, base_id=i + 1)
    pred = predict_wall(session, "p1", "python t.py")
    assert isinstance(pred, WallPrediction)
    # Most recent (highest IDs) had wall=1000s
    assert pred.n_samples == HISTORY_WINDOW
    assert pred.p50_s == pytest.approx(1000.0)


# ---------------- remaining_for_running ----------------


def test_remaining_for_running_subtracts_elapsed():
    pred = WallPrediction(p50_s=600.0, p90_s=900.0, n_samples=10, clipped=False)
    started = datetime(2026, 4, 1, tzinfo=UTC)
    now = started + timedelta(seconds=300)
    job = Job(
        project="p1",
        host_pin="any",
        priority=50,
        state=JobState.RUNNING.value,
        cmd_json="[]",
        cwd="/tmp",
        env_json="{}",
        submitted_at=started,
        started_at=started,
    )
    rem_p50, rem_p90 = remaining_for_running(job, pred, now)
    assert rem_p50 == pytest.approx(300.0)
    assert rem_p90 == pytest.approx(600.0)


def test_remaining_for_running_floors_at_zero():
    pred = WallPrediction(p50_s=60.0, p90_s=120.0, n_samples=10, clipped=False)
    started = datetime(2026, 4, 1, tzinfo=UTC)
    now = started + timedelta(seconds=99999)
    job = Job(
        project="p1",
        host_pin="any",
        priority=50,
        state=JobState.RUNNING.value,
        cmd_json="[]",
        cwd="/tmp",
        env_json="{}",
        submitted_at=started,
        started_at=started,
    )
    rem_p50, rem_p90 = remaining_for_running(job, pred, now)
    assert rem_p50 == 0.0
    assert rem_p90 == 0.0


# ---------------- queue_start_eta ----------------


def test_queue_start_eta_no_competition_returns_zero(session):
    cache = make_predict_cache(session)
    target = Job(
        id=1,
        project="p1",
        host_pin="any",
        priority=50,
        state=JobState.QUEUED.value,
        cmd_json=json.dumps(["python", "t.py"]),
        cwd="/tmp",
        env_json="{}",
        submitted_at=datetime(2026, 4, 1, tzinfo=UTC),
    )
    now = datetime(2026, 4, 1, tzinfo=UTC)
    eta = queue_start_eta(target, [], [], cache, now)
    assert eta == 0.0


def test_queue_start_eta_sums_running_remaining(session):
    # Build history so prediction exists for the running job's (project, head).
    for i in range(5):
        _add_completed(session, "p1", ["python", "t.py"], 600.0, base_id=i + 1)
    cache = make_predict_cache(session)
    started = datetime(2026, 4, 1, tzinfo=UTC)
    running = Job(
        id=10,
        project="p1",
        host_pin="any",
        priority=50,
        state=JobState.RUNNING.value,
        cmd_json=json.dumps(["python", "t.py"]),
        cwd="/tmp",
        env_json="{}",
        submitted_at=started,
        started_at=started,
    )
    target = Job(
        id=11,
        project="p1",
        host_pin="any",
        priority=50,
        state=JobState.QUEUED.value,
        cmd_json=json.dumps(["python", "t.py"]),
        cwd="/tmp",
        env_json="{}",
        submitted_at=started,
    )
    now = started + timedelta(seconds=200)
    eta = queue_start_eta(target, [target], [running], cache, now)
    # 600 - 200 elapsed = 400s remaining on the running job.
    assert eta == pytest.approx(400.0)


def test_queue_start_eta_skips_lower_priority_queued(session):
    for i in range(5):
        _add_completed(session, "p1", ["python", "t.py"], 100.0, base_id=i + 1)
    cache = make_predict_cache(session)
    target = Job(
        id=20,
        project="p1",
        host_pin="any",
        priority=50,
        state=JobState.QUEUED.value,
        cmd_json=json.dumps(["python", "t.py"]),
        cwd="/tmp",
        env_json="{}",
        submitted_at=datetime(2026, 4, 1, tzinfo=UTC),
    )
    lower = Job(
        id=21,
        project="p1",
        host_pin="any",
        priority=10,  # lower than target
        state=JobState.QUEUED.value,
        cmd_json=json.dumps(["python", "t.py"]),
        cwd="/tmp",
        env_json="{}",
        submitted_at=datetime(2026, 4, 1, tzinfo=UTC),
    )
    now = datetime(2026, 4, 1, tzinfo=UTC)
    eta = queue_start_eta(target, [target, lower], [], cache, now)
    # Lower-priority queued job is behind target → not counted.
    assert eta == 0.0


def test_queue_start_eta_respects_host_pin(session):
    for i in range(5):
        _add_completed(session, "p1", ["python", "t.py"], 600.0, base_id=i + 1)
    cache = make_predict_cache(session)
    started = datetime(2026, 4, 1, tzinfo=UTC)
    running_other = Job(
        id=30,
        project="p1",
        host_pin="laptop",  # different specific host
        priority=50,
        state=JobState.RUNNING.value,
        cmd_json=json.dumps(["python", "t.py"]),
        cwd="/tmp",
        env_json="{}",
        submitted_at=started,
        started_at=started,
    )
    target = Job(
        id=31,
        project="p1",
        host_pin="desktop",
        priority=50,
        state=JobState.QUEUED.value,
        cmd_json=json.dumps(["python", "t.py"]),
        cwd="/tmp",
        env_json="{}",
        submitted_at=started,
    )
    eta = queue_start_eta(target, [target], [running_other], cache, started)
    # Different non-any host pins → not competing → no wait.
    assert eta == 0.0


def test_constants_sane():
    assert MIN_SAMPLES >= 3
    assert 0 < CLIP_THRESHOLD <= 1.0
    assert HISTORY_WINDOW >= MIN_SAMPLES
