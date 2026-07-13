"""`job_log_path` must keep the log file inside logs_dir, whatever it is handed.

Four places used to compose `logs_dir / f"{job_id}.log"` by hand and had already drifted
(two raw interpolations, one ad-hoc `int()` cast, one differently-named variable). The
traversal was not reachable in practice — FastAPI types `job_id` as `int`, so `../` is
rejected with a 422 several frames earlier — but that made the safety a property of an
annotation far from the filesystem call, and invisible where it mattered. These tests
pin the guarantee locally instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobd.broker.joblog import job_log_path


def test_builds_the_expected_path(tmp_path: Path):
    assert job_log_path(tmp_path, 42) == tmp_path / "42.log"


def test_numeric_string_is_accepted_and_normalized(tmp_path: Path):
    assert job_log_path(tmp_path, "42") == tmp_path / "42.log"


@pytest.mark.parametrize(
    "hostile",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "1/../../../etc/passwd",
        "..",
        "1; rm -rf /",
    ],
)
def test_traversal_attempts_are_rejected_not_composed(tmp_path: Path, hostile: str):
    """A non-integer job id must raise, not become part of a path."""
    with pytest.raises(ValueError):
        job_log_path(tmp_path, hostile)


def test_result_never_escapes_logs_dir(tmp_path: Path):
    """Whatever is accepted resolves to a direct child of logs_dir."""
    for job_id in (0, 1, 999999, "7"):
        p = job_log_path(tmp_path, job_id).resolve()
        assert p.parent == tmp_path.resolve(), f"{p} escaped {tmp_path}"
