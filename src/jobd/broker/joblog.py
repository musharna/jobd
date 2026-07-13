"""One safe way to build a job's log path.

Four different places were composing `logs_dir / f"{job_id}.log"` by hand, and they had
already drifted: two interpolated the raw value, one had grown an ad-hoc `int()` cast to
quiet a static-analysis warning, and the sweeper spelled the variable differently again.
Four spellings of one rule is how the fifth one gets it wrong.

`job_id` reaches the broker as a path parameter. FastAPI types it `int`, so a value
carrying `../` is rejected with a 422 long before it reaches a filesystem call — the
traversal is not actually reachable today. But that safety is a property of a type
annotation several call-frames away, which is a thin thing to rest on and invisible at
the point of use. Coercing here makes the guarantee local and total: whatever a caller
passes, the filename that reaches the filesystem is a bare integer plus ".log", so it
cannot escape `logs_dir` no matter how the callers evolve.
"""

from __future__ import annotations

from pathlib import Path


def job_log_path(logs_dir: Path, job_id: int | str) -> Path:
    """Return the log file for `job_id`, inside `logs_dir` and nowhere else.

    Raises ValueError if `job_id` is not an integer — a caller that has lost track of
    what it is holding should fail loudly rather than compose a path out of it.
    """
    return logs_dir / f"{int(job_id)}.log"
