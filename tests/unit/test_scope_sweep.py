"""SIGTERM-drain Phase 3 (docs/plans/sigterm-drain.md): startup scope sweep.

After an undrained worker death, scope-wrapped workloads survive in their
jobd-<id>.scope units (scopes live outside the worker service's cgroup). If
the broker requeued their jobs (Phase 2 reconcile), a re-dispatch would
double-execute against the still-running old workload. The restarted worker
therefore kills every leftover jobd-*.scope at startup, before its first
poll — any such scope predates this incarnation and is by definition stale
(one jobd worker per user session).
"""

import subprocess

import jobd.worker.job_worker as job_worker

job_worker.set_scope_namespace("http://127.0.0.1:8765")
_U101, _U102, _U77 = (job_worker.scope_unit_name(n) for n in (101, 102, 77))

_LIST_UNITS_TWO = (
    f"{_U101} loaded active running [systemd-run] bash -c sleep 999\n"
    f"{_U102} loaded inactive dead [systemd-run] python train.py\n"
)


def _fake_run_factory(list_output: str, calls: list[list[str]]):
    def _fake_run(args, **_kw):
        calls.append(list(args))

        class R:
            returncode = 0
            stdout = list_output if "list-units" in args else ""

        return R()

    return _fake_run


def test_sweep_kills_every_listed_scope(monkeypatch, tmp_path):
    job_worker.set_scope_namespace("http://127.0.0.1:8765")
    monkeypatch.setattr(job_worker.shutil, "which", lambda _n: "/fake/bin/systemctl")
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(_LIST_UNITS_TWO, calls))

    resolved: dict[str, object] = {}
    killed: list[str] = []
    for unit in (_U101, _U102):
        d = tmp_path / unit
        d.mkdir()
        (d / "cgroup.procs").write_text("1234\n")
        resolved[unit] = d
    monkeypatch.setattr(
        job_worker._cgroup_walk, "resolve_user_scope_path", lambda unit: resolved.get(unit)
    )
    monkeypatch.setattr(
        job_worker._cgroup_walk,
        "kill_scope",
        lambda path: killed.append(path.name) or [1234],
    )

    swept = job_worker._sweep_stale_scopes()

    assert sorted(swept) == sorted([_U101, _U102])
    assert sorted(killed) == sorted([_U101, _U102])


def test_sweep_ignores_non_jobd_units(monkeypatch):
    """Pattern is strictly jobd-<digits>.scope — a user's unrelated scopes
    (or a malformed name) must never be killed."""
    output = (
        "app-firefox-1234.scope loaded active running Firefox\n"
        "jobd-abc.scope loaded active running not-ours\n"
        "jobdx-7.scope loaded active running not-ours-either\n"
    )
    monkeypatch.setattr(job_worker.shutil, "which", lambda _n: "/fake/bin/systemctl")
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(output, calls))
    killed: list[str] = []
    monkeypatch.setattr(job_worker._cgroup_walk, "kill_scope", lambda p: killed.append(p) or [])

    assert job_worker._sweep_stale_scopes() == []
    assert killed == []
    # Only the enumeration call — no kill invocations of any kind.
    assert all("kill" not in c for c in calls)


def test_sweep_no_systemctl_is_a_noop(monkeypatch):
    monkeypatch.setattr(job_worker.shutil, "which", lambda _n: None)

    def _boom(*_a, **_kw):
        raise AssertionError("must not invoke subprocess without systemctl")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert job_worker._sweep_stale_scopes() == []


def test_sweep_survives_list_units_failure(monkeypatch):
    monkeypatch.setattr(job_worker.shutil, "which", lambda _n: "/fake/bin/systemctl")

    def _boom(*_a, **_kw):
        raise OSError("bus unavailable")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert job_worker._sweep_stale_scopes() == []


def test_sweep_falls_back_to_systemctl_kill_without_cgroup_path(monkeypatch):
    job_worker.set_scope_namespace("http://127.0.0.1:8765")
    """CI containers and odd cgroup layouts can defeat the path resolve; the
    sweep then kills through systemctl directly rather than skipping."""
    output = f"{_U77} loaded active running [systemd-run] sleep 999\n"
    monkeypatch.setattr(job_worker.shutil, "which", lambda _n: "/fake/bin/systemctl")
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(output, calls))
    monkeypatch.setattr(job_worker._cgroup_walk, "resolve_user_scope_path", lambda _u: None)

    swept = job_worker._sweep_stale_scopes()

    assert swept == [_U77]
    kill_calls = [c for c in calls if "kill" in c]
    assert len(kill_calls) == 1
    assert kill_calls[0][-1] == _U77
    assert "--signal=KILL" in kill_calls[0]


# --- ownership: a worker may only sweep what its own broker's workers made ---
#
# Until 0.5.46 the unit name carried only the job id and the sweep killed every
# jobd-<digits>.scope in the user session. A second worker under the same uid
# (an isolated test broker, the JOBD_LIVE suite) therefore SIGKILLed the
# production worker's running jobs, and two brokers issuing the same job id
# collided on one unit name.


def test_scope_name_carries_the_brokers_namespace():
    a = job_worker.scope_namespace("http://broker.example:8765")
    b = job_worker.scope_namespace("http://127.0.0.1:18765")
    assert a != b
    assert job_worker.scope_namespace("HTTP://broker.example:8765/") == a
    job_worker.set_scope_namespace("http://broker.example:8765")
    assert job_worker.scope_unit_name(42) == f"jobd-{a}-42.scope"
    job_worker.set_scope_namespace("http://127.0.0.1:18765")
    assert job_worker.scope_unit_name(42) == f"jobd-{b}-42.scope"


def test_sweep_leaves_another_brokers_and_unattributed_scopes_alone(monkeypatch, caplog):
    job_worker.set_scope_namespace("http://127.0.0.1:18765")
    mine = job_worker.scope_unit_name(5)
    foreign = f"jobd-{job_worker.scope_namespace('http://broker.example:8765')}-5.scope"
    legacy = "jobd-3877.scope"
    output = "".join(
        f"{u} loaded active running [systemd-run] sleep 999\n" for u in (foreign, mine, legacy)
    )
    monkeypatch.setattr(job_worker.shutil, "which", lambda _n: "/fake/bin/systemctl")
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(output, calls))
    resolved: list[str] = []
    monkeypatch.setattr(
        job_worker._cgroup_walk,
        "resolve_user_scope_path",
        lambda u: resolved.append(u) or f"/cg/{u}",
    )
    killed: list[str] = []
    monkeypatch.setattr(job_worker._cgroup_walk, "kill_scope", lambda p: killed.append(p) or [1])

    with caplog.at_level("WARNING"):
        swept = job_worker._sweep_stale_scopes()

    # Positive control first: its own stale scope IS killed...
    assert swept == [mine]
    assert killed == [f"/cg/{mine}"]
    # ...and nothing else is touched by either kill path.
    assert resolved == [mine]
    assert not [c for c in calls if "kill" in c]
    # Left alone is not left unsaid.
    assert foreign in caplog.text and legacy in caplog.text
