"""Wall-time prediction + queue-aware ETA for jobd.

V1 design:
- Buckets: (project, cmd_head). cmd_head extracts a stable string from cmd
  (Path(argv[0]).name, plus next non-flag token for known wrappers).
- Source: existing jobs.db `started_at` / `finished_at` on completed jobs.
- Cold-start: <3 historical samples → return None for the bucket.
- Clip detection: any sample that ran within CLIP_THRESHOLD of its
  --max-wall cap is flagged so callers can warn "upper-bound clipped".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobd.db import Job
from jobd.models import JobState

HISTORY_WINDOW = 50
MIN_SAMPLES = 3
CLIP_THRESHOLD = 0.95
CMD_HEAD_MAX = 80
WRAPPERS = frozenset({"python", "python3", "python2", "Rscript", "R", "uv", "uvx", "bash", "sh"})


def cmd_head(cmd: list[str]) -> str:
    """Bucket key: stable digest of a command for time-estimation grouping.

    Examples:
        ["python", "train.py", "--seed", "0"]      -> "python train.py"
        ["/usr/bin/python3", "/abs/path/run.py"]   -> "python3 run.py"
        ["python", "-m", "torch.distributed.run"]  -> "python -m torch.distributed.run"
        ["Rscript", "pipeline.R"]                  -> "Rscript pipeline.R"
        ["./train.sh"]                             -> "train.sh"
        ["bash", "-c", "..."]                      -> "bash -c"
        []                                         -> ""
    """
    if not cmd:
        return ""
    head = Path(cmd[0]).name or cmd[0]
    if head not in WRAPPERS or len(cmd) < 2:
        return head[:CMD_HEAD_MAX]

    rest = cmd[1:]
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "-m" and i + 1 < len(rest):
            return f"{head} -m {rest[i + 1]}"[:CMD_HEAD_MAX]
        if tok == "-c":
            return f"{head} -c"[:CMD_HEAD_MAX]
        if tok.startswith("-"):
            i += 1
            continue
        return f"{head} {Path(tok).name}"[:CMD_HEAD_MAX]
    return head[:CMD_HEAD_MAX]


def _quantile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated quantile. Empty list → 0.0."""
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_vals[0]
    pos = (n - 1) * q
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


@dataclass(frozen=True)
class WallPrediction:
    p50_s: float
    p90_s: float
    n_samples: int
    clipped: bool

    @property
    def basis(self) -> str:
        return f"history-N={self.n_samples}"


@dataclass(frozen=True)
class ColdStart:
    n_samples: int

    @property
    def basis(self) -> str:
        return f"insufficient-history-N={self.n_samples}"


def predict_wall(session: Session, project: str, head: str) -> WallPrediction | ColdStart:
    """Predict wall-time for a (project, cmd_head) bucket from history.

    Pulls completed jobs in this project (filtered to matching cmd_head in
    Python — cheap because HISTORY_WINDOW * 4 bound), computes p50/p90 on
    the most recent HISTORY_WINDOW matches, flags clipping.
    """
    if not head:
        return ColdStart(n_samples=0)

    stmt = (
        select(Job)
        .where(
            Job.project == project,
            Job.state == JobState.COMPLETED.value,
            Job.started_at.is_not(None),
            Job.finished_at.is_not(None),
        )
        .order_by(Job.id.desc())
        .limit(HISTORY_WINDOW * 4)
    )
    walls: list[float] = []
    clipped = 0
    for job in session.execute(stmt).scalars():
        try:
            row_cmd = json.loads(job.cmd_json)
        except (TypeError, ValueError):
            continue
        if cmd_head(row_cmd) != head:
            continue
        if job.started_at is None or job.finished_at is None:
            continue
        wall = (job.finished_at - job.started_at).total_seconds()
        if wall <= 0:
            continue
        walls.append(wall)
        if job.max_wall_s and wall >= CLIP_THRESHOLD * job.max_wall_s:
            clipped += 1
        if len(walls) >= HISTORY_WINDOW:
            break

    n = len(walls)
    if n < MIN_SAMPLES:
        return ColdStart(n_samples=n)
    walls.sort()
    return WallPrediction(
        p50_s=_quantile(walls, 0.5),
        p90_s=_quantile(walls, 0.9),
        n_samples=n,
        clipped=clipped > 0,
    )


def remaining_for_running(
    job: Job, prediction: WallPrediction, now: datetime
) -> tuple[float, float]:
    """For a running job: (p50_remaining, p90_remaining), floored at 0."""
    if job.started_at is None:
        return prediction.p50_s, prediction.p90_s
    started = job.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    elapsed = (now - started).total_seconds()
    return max(0.0, prediction.p50_s - elapsed), max(0.0, prediction.p90_s - elapsed)


@dataclass
class _PredictCache:
    session: Session
    cache: dict[tuple[str, str], WallPrediction | ColdStart]

    def get(self, project: str, head: str) -> WallPrediction | ColdStart:
        key = (project, head)
        if key not in self.cache:
            self.cache[key] = predict_wall(self.session, project, head)
        return self.cache[key]


def make_predict_cache(session: Session) -> _PredictCache:
    return _PredictCache(session=session, cache={})


def queue_start_eta(
    target: Job,
    queued_jobs: Iterable[Job],
    running_jobs: Iterable[Job],
    cache: _PredictCache,
    now: datetime,
) -> float | None:
    """Estimate seconds until `target` (a queued job) starts.

    V1 model: eligible-worker set is approximated by host_pin equality (or
    `any`-pinned jobs share a single pool). For each running job competing
    for the same pool, sum max(0, p50 - elapsed). For each queued job ahead
    of `target` in the same pool with priority >= target's, sum p50.

    Returns None when no prediction is available for the target itself
    (no point reporting a start ETA without a wall ETA to add to it).
    """
    if target.state != JobState.QUEUED.value:
        return None

    target_pool = target.host_pin
    target_priority = target.priority

    def shares_pool(other: Job) -> bool:
        if target_pool == "any" or other.host_pin == "any":
            return True
        return other.host_pin == target_pool

    total = 0.0
    counted = False
    for r in running_jobs:
        if not shares_pool(r):
            continue
        pred = cache.get(r.project, cmd_head(json.loads(r.cmd_json)))
        if not isinstance(pred, WallPrediction):
            continue
        rem_p50, _ = remaining_for_running(r, pred, now)
        total += rem_p50
        counted = True

    for q in queued_jobs:
        if q.id == target.id:
            continue
        if not shares_pool(q):
            continue
        # Higher priority value = scheduled first (matcher uses priority desc).
        if q.priority < target_priority:
            continue
        if q.priority == target_priority and q.id > target.id:
            continue
        pred = cache.get(q.project, cmd_head(json.loads(q.cmd_json)))
        if not isinstance(pred, WallPrediction):
            continue
        total += pred.p50_s
        counted = True

    if not counted:
        # No competition or no predictable competition → start immediately.
        return 0.0
    return total
