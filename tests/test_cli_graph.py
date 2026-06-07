"""Tests for `job graph` cross-project DAG view."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _now_iso(offset_seconds: int = 0) -> str:
    return (datetime.now(UTC) + timedelta(seconds=offset_seconds)).isoformat()


def _job(
    job_id: int,
    *,
    state: str = "completed",
    project: str = "project-c",
    cmd: list[str] | None = None,
    depends_on: list[int] | None = None,
    age_seconds: int = 60,
) -> dict:
    return {
        "id": job_id,
        "state": state,
        "project": project,
        "cmd": cmd or ["python", "-m", "project-c"],
        "depends_on": depends_on or [],
        "submitted_at": _now_iso(-age_seconds),
    }


# ---------------------------------------------------------------------------
# _parse_since
# ---------------------------------------------------------------------------


def test_parse_since_hours():
    from job_cli.cli import _parse_since

    assert _parse_since("24h") == timedelta(hours=24)


def test_parse_since_days():
    from job_cli.cli import _parse_since

    assert _parse_since("3d") == timedelta(days=3)


def test_parse_since_weeks():
    from job_cli.cli import _parse_since

    assert _parse_since("2w") == timedelta(weeks=2)


def test_parse_since_rejects_garbage():
    import typer

    from job_cli.cli import _parse_since

    with pytest.raises(typer.BadParameter):
        _parse_since("forever")


def test_parse_since_rejects_zero():
    import typer

    from job_cli.cli import _parse_since

    with pytest.raises(typer.BadParameter):
        _parse_since("0h")


# ---------------------------------------------------------------------------
# _filter_recent
# ---------------------------------------------------------------------------


def test_filter_recent_drops_old():
    from job_cli.cli import _filter_recent

    jobs = [
        _job(1, age_seconds=60),  # recent
        _job(2, age_seconds=3600 * 48),  # 2d old
    ]
    out = _filter_recent(jobs, timedelta(hours=24))
    assert [j["id"] for j in out] == [1]


def test_filter_recent_keeps_all_when_window_wide():
    from job_cli.cli import _filter_recent

    jobs = [_job(1, age_seconds=3600 * 48), _job(2, age_seconds=60)]
    out = _filter_recent(jobs, timedelta(weeks=4))
    assert {j["id"] for j in out} == {1, 2}


# ---------------------------------------------------------------------------
# _orphan_ids
# ---------------------------------------------------------------------------


def test_orphan_ids_flags_child_of_failed_parent():
    from job_cli.cli import _orphan_ids

    jobs = [
        _job(1, state="failed"),
        _job(2, state="cancelled", depends_on=[1]),
    ]
    assert _orphan_ids(jobs) == {2}


def test_orphan_ids_does_not_flag_completed_chain():
    from job_cli.cli import _orphan_ids

    jobs = [
        _job(1, state="completed"),
        _job(2, state="completed", depends_on=[1]),
    ]
    assert _orphan_ids(jobs) == set()


def test_orphan_ids_flags_child_of_preempted_or_cancelled():
    from job_cli.cli import _orphan_ids

    jobs = [
        _job(1, state="preempted"),
        _job(2, state="cancelled", depends_on=[1]),
        _job(3, state="cancelled"),
        _job(4, state="cancelled", depends_on=[3]),
    ]
    assert _orphan_ids(jobs) == {2, 4}


def test_orphan_ids_unknown_parent_not_orphan():
    """If the parent isn't in the filtered window, don't speculate."""
    from job_cli.cli import _orphan_ids

    jobs = [_job(2, state="cancelled", depends_on=[999])]
    assert _orphan_ids(jobs) == set()


# ---------------------------------------------------------------------------
# _render_ascii
# ---------------------------------------------------------------------------


def test_render_ascii_shows_root_and_indented_child():
    from job_cli.cli import _render_ascii

    jobs = [
        _job(10, state="completed", project="project-c", cmd=["fit_sweep"]),
        _job(
            11,
            state="completed",
            project="project-c",
            cmd=["evaluate"],
            depends_on=[10],
        ),
    ]
    out = _render_ascii(jobs, orphan_ids=set())
    lines = out.splitlines()
    # Root appears before child; child is indented
    root_idx = next(i for i, ln in enumerate(lines) if "10" in ln and "fit_sweep" in ln)
    child_idx = next(i for i, ln in enumerate(lines) if "11" in ln and "evaluate" in ln)
    assert root_idx < child_idx
    assert lines[child_idx].startswith(" ")  # indented


def test_render_ascii_marks_orphan():
    from job_cli.cli import _render_ascii

    jobs = [
        _job(20, state="failed", project="project-c", cmd=["fit"]),
        _job(
            21,
            state="cancelled",
            project="project-c",
            cmd=["downstream"],
            depends_on=[20],
        ),
    ]
    out = _render_ascii(jobs, orphan_ids={21})
    assert "orphaned" in out.lower() or "parent_failed" in out.lower()


def test_render_ascii_handles_multi_parent_fanin():
    """A job depending on two parents is referenced under the second parent."""
    from job_cli.cli import _render_ascii

    jobs = [
        _job(30, project="a", cmd=["a"]),
        _job(31, project="b", cmd=["b"]),
        _job(32, project="c", cmd=["c"], depends_on=[30, 31]),
    ]
    out = _render_ascii(jobs, orphan_ids=set())
    # "32" appears at least twice (once full, once as a fan-in ref) OR
    # the output contains an explicit fan-in marker; we accept either.
    occurrences = sum(1 for ln in out.splitlines() if "32" in ln)
    assert occurrences >= 2 or "fan-in" in out.lower() or "…" in out


# ---------------------------------------------------------------------------
# _render_dot
# ---------------------------------------------------------------------------


def test_render_dot_emits_digraph_header():
    from job_cli.cli import _render_dot

    jobs = [_job(1)]
    out = _render_dot(jobs, orphan_ids=set())
    assert out.lstrip().startswith("digraph")
    assert "}" in out


def test_render_dot_emits_edge_for_dependency():
    from job_cli.cli import _render_dot

    jobs = [_job(1), _job(2, depends_on=[1])]
    out = _render_dot(jobs, orphan_ids=set())
    # Edge: 1 -> 2 (parent points to child)
    assert "1 -> 2" in out or "1->2" in out


def test_render_dot_colors_by_state():
    from job_cli.cli import _render_dot

    jobs = [_job(1, state="completed"), _job(2, state="failed")]
    out = _render_dot(jobs, orphan_ids=set())
    # Both nodes get a color attribute somewhere in their attribute block
    assert "color=" in out
    # Distinct colors for distinct states
    assert "green" in out.lower()
    assert "red" in out.lower()


# ---------------------------------------------------------------------------
# end-to-end CLI
# ---------------------------------------------------------------------------


def _patch_client(monkeypatch, jobs):
    import job_cli.cli as cli_mod

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, path, params=None):
            if path == "/jobs":
                # Mirror the real broker's server-side filters (see jobd.app /jobs).
                filtered = jobs
                if params and params.get("project"):
                    filtered = [j for j in filtered if j["project"] == params["project"]]
                if params and params.get("state_filter"):
                    filtered = [j for j in filtered if j["state"] == params["state_filter"]]
                return _FakeResp(filtered)
            if path == "/workers":
                return _FakeResp([])
            return _FakeResp([])

    monkeypatch.setattr(cli_mod, "_client", lambda: FakeClient())
    return cli_mod


def test_graph_command_renders_ascii_by_default(monkeypatch):
    cli_mod = _patch_client(
        monkeypatch,
        [
            _job(100, project="project-c", cmd=["fit_sweep"]),
            _job(101, project="project-c", cmd=["evaluate"], depends_on=[100]),
        ],
    )
    r = CliRunner().invoke(cli_mod.app, ["graph"])
    assert r.exit_code == 0, r.stdout
    assert "100" in r.stdout
    assert "101" in r.stdout


def test_graph_command_filters_by_project(monkeypatch):
    cli_mod = _patch_client(
        monkeypatch,
        [
            _job(200, project="project-c", cmd=["a"]),
            _job(201, project="project-a", cmd=["b"]),
        ],
    )
    r = CliRunner().invoke(cli_mod.app, ["graph", "--project", "project-c"])
    assert r.exit_code == 0, r.stdout
    assert "200" in r.stdout
    assert "201" not in r.stdout


def test_graph_command_format_dot(monkeypatch):
    cli_mod = _patch_client(monkeypatch, [_job(300)])
    r = CliRunner().invoke(cli_mod.app, ["graph", "--format", "dot"])
    assert r.exit_code == 0, r.stdout
    assert "digraph" in r.stdout


def test_graph_command_since_drops_old_jobs(monkeypatch):
    cli_mod = _patch_client(
        monkeypatch,
        [
            _job(400, age_seconds=60),
            _job(401, age_seconds=3600 * 48),
        ],
    )
    r = CliRunner().invoke(cli_mod.app, ["graph", "--since", "24h"])
    assert r.exit_code == 0, r.stdout
    assert "400" in r.stdout
    assert "401" not in r.stdout


def test_graph_command_rejects_bad_since(monkeypatch):
    cli_mod = _patch_client(monkeypatch, [_job(500)])
    r = CliRunner().invoke(cli_mod.app, ["graph", "--since", "forever"])
    assert r.exit_code != 0
