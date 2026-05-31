"""CLI tests — smoke only; deeper API tests are in test_api.py."""

from typer.testing import CliRunner

from job_cli.cli import app

runner = CliRunner()


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakePostClient:
    """Minimal JobdClient stub for `job submit` tests. Captures the request
    path + body, returns a configurable JSON response. Routing the CLI
    through JobdClient (instead of bare httpx.post) means the Bearer
    header injection is automatic — tests just need to stub _client()."""

    def __init__(self, captured: dict, response: dict | None = None):
        import httpx

        self._captured = captured
        self._response = httpx.Response(200, json=response if response is not None else {"id": 1})

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def post(self, path, *, json=None, params=None):
        self._captured["path"] = path
        self._captured["body"] = json
        return self._response


def test_cli_help():
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    assert "submit" in r.stdout


def test_status_oneshot_renders_and_exits(monkeypatch):
    """`job status <id>` prints rendered state and exits 0 for running job."""

    import job_cli.cli as cli_mod

    fake_job = {
        "id": 42,
        "state": "running",
        "project": "p",
        "priority": 55,
        "host_pin": "any",
        "worker": "desktop",
        "preemptible": False,
        "submitted_at": "2026-04-24T00:00:00+00:00",
        "started_at": "2026-04-24T00:01:00+00:00",
        "finished_at": None,
        "exit_code": None,
        "cmd": ["echo", "hi"],
        "warning": None,
    }

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, path):
            assert path == "/jobs/42"
            return _FakeResp(fake_job)

    monkeypatch.setattr(cli_mod, "_client", lambda: FakeClient())
    r = CliRunner().invoke(cli_mod.app, ["status", "42"])
    assert r.exit_code == 0
    assert "state=running" in r.stdout
    assert "project=p" in r.stdout
    assert "worker=desktop" in r.stdout


def test_status_terminal_propagates_exit_code(monkeypatch):
    """Terminal job with exit_code=3 should exit 3."""

    import job_cli.cli as cli_mod

    fake_job = {
        "id": 7,
        "state": "failed",
        "project": "p",
        "priority": 55,
        "host_pin": "any",
        "worker": "desktop",
        "preemptible": False,
        "submitted_at": "2026-04-24T00:00:00+00:00",
        "started_at": "2026-04-24T00:01:00+00:00",
        "finished_at": "2026-04-24T00:02:00+00:00",
        "exit_code": 3,
        "cmd": ["false"],
        "warning": None,
    }

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, path):
            return _FakeResp(fake_job)

    monkeypatch.setattr(cli_mod, "_client", lambda: FakeClient())
    r = CliRunner().invoke(cli_mod.app, ["status", "7"])
    assert r.exit_code == 3


def test_list_banner_silent_when_all_fresh(monkeypatch):
    """`job list` with only fresh workers → no banner."""
    import job_cli.cli as cli_mod
    from datetime import datetime, timezone

    recent = datetime.now(timezone.utc).isoformat()
    workers = [{"host": "desktop", "last_heartbeat": recent, "state": "online"}]

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, path, params=None):
            if path == "/workers":
                return _FakeResp(workers)
            return _FakeResp([])

    monkeypatch.setattr(cli_mod, "_client", lambda: FakeClient())
    r = CliRunner().invoke(cli_mod.app, ["list"])
    assert r.exit_code == 0
    assert "⚠" not in r.stdout


def test_list_banner_warns_on_stale_worker(monkeypatch):
    """`job list` with a 120s-stale heartbeat → banner."""
    import job_cli.cli as cli_mod
    from datetime import datetime, timedelta, timezone

    stale = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    workers = [{"host": "laptop", "last_heartbeat": stale, "state": "online"}]

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, path, params=None):
            if path == "/workers":
                return _FakeResp(workers)
            return _FakeResp([])

    monkeypatch.setattr(cli_mod, "_client", lambda: FakeClient())
    r = CliRunner().invoke(cli_mod.app, ["list"])
    assert r.exit_code == 0
    assert "worker health" in r.stdout
    assert "laptop" in r.stdout


def test_list_banner_warns_on_no_workers(monkeypatch):
    """`job list` with empty fleet → 'no workers registered' banner."""
    import job_cli.cli as cli_mod

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, path, params=None):
            if path == "/workers":
                return _FakeResp([])
            return _FakeResp([])

    monkeypatch.setattr(cli_mod, "_client", lambda: FakeClient())
    r = CliRunner().invoke(cli_mod.app, ["list"])
    assert r.exit_code == 0
    assert "no workers registered" in r.stdout


def test_submit_builds_requires_from_flags(monkeypatch):
    """--needs / --arch / --os / --gpu should populate the requires block."""
    from typer.testing import CliRunner
    import job_cli.cli as cli_mod

    captured: dict = {}
    monkeypatch.setattr(cli_mod, "_client", lambda: _FakePostClient(captured))

    runner = CliRunner()
    r = runner.invoke(
        cli_mod.app,
        [
            "submit",
            "--project",
            "p",
            "--cwd",
            "/tmp",
            "--needs",
            "R",
            "--needs",
            "python3",
            "--arch",
            "arm64",
            "--gpu",
            "--idempotent",
            "--",
            "echo",
            "hi",
        ],
    )
    assert r.exit_code == 0
    assert captured["path"] == "/submit"
    body = captured["body"]
    assert body["requires"] == {
        "arch": "arm64",
        "os": "any",
        "gpu": True,
        "needs": ["R", "python3"],
        "idempotent": True,
    }


def test_submit_passes_depends_on_flags(monkeypatch):
    """--depends-on (repeatable) + --depends-on-any-exit populate body."""
    import job_cli.cli as cli_mod

    captured: dict = {}
    monkeypatch.setattr(cli_mod, "_client", lambda: _FakePostClient(captured))

    r = CliRunner().invoke(
        cli_mod.app,
        [
            "submit",
            "--project",
            "p",
            "--cwd",
            "/tmp",
            "--depends-on",
            "10",
            "--depends-on",
            "11",
            "--depends-on-any-exit",
            "--",
            "echo",
            "hi",
        ],
    )
    assert r.exit_code == 0, r.output
    assert captured["body"]["depends_on"] == [10, 11]
    assert captured["body"]["depends_on_any_exit"] is True


def test_submit_passes_max_wall_and_idle_timeout(monkeypatch):
    """--max-wall / --idle-timeout populate the JobSubmit body fields."""
    import job_cli.cli as cli_mod

    captured: dict = {}
    monkeypatch.setattr(cli_mod, "_client", lambda: _FakePostClient(captured))

    r = CliRunner().invoke(
        cli_mod.app,
        [
            "submit",
            "--project",
            "p",
            "--cwd",
            "/tmp",
            "--max-wall",
            "30",
            "--idle-timeout",
            "10",
            "--",
            "sleep",
            "300",
        ],
    )
    assert r.exit_code == 0, r.output
    assert captured["body"]["max_wall_s"] == 30
    assert captured["body"]["idle_timeout_s"] == 10


def test_submit_passes_checkpoint_grace(monkeypatch):
    """#39: --checkpoint-grace populates checkpoint_grace_s on the body."""
    import job_cli.cli as cli_mod

    captured: dict = {}
    monkeypatch.setattr(cli_mod, "_client", lambda: _FakePostClient(captured))

    r = CliRunner().invoke(
        cli_mod.app,
        [
            "submit",
            "--project",
            "p",
            "--cwd",
            "/tmp",
            "--checkpoint-grace",
            "120",
            "--",
            "echo",
            "hi",
        ],
    )
    assert r.exit_code == 0, r.output
    assert captured["body"]["checkpoint_grace_s"] == 120


def test_submit_omits_timeout_fields_when_unset(monkeypatch):
    """No --max-wall / --idle-timeout / --checkpoint-grace → keys absent
    from body (not null) so broker can apply project/profile defaults."""
    import job_cli.cli as cli_mod

    captured: dict = {}
    monkeypatch.setattr(cli_mod, "_client", lambda: _FakePostClient(captured))

    r = CliRunner().invoke(
        cli_mod.app,
        ["submit", "--project", "p", "--cwd", "/tmp", "--", "echo", "hi"],
    )
    assert r.exit_code == 0, r.output
    assert "max_wall_s" not in captured["body"]
    assert "idle_timeout_s" not in captured["body"]
    assert "checkpoint_grace_s" not in captured["body"]


def test_submit_eta_banner_prints_p50_p90_when_history(monkeypatch):
    """BACKLOG Part 1: default-on banner prints p50/p90/n when broker reports
    history. ~30 LOC follow-on to per-job time estimation v1."""
    import job_cli.cli as cli_mod

    captured: dict = {}
    monkeypatch.setattr(
        cli_mod,
        "_client",
        lambda: _FakePostClient(
            captured,
            response={
                "id": 42,
                "eta_p50_s": 600.0,
                "eta_p90_s": 1200.0,
                "eta_basis": "history-N=8",
                "eta_clipped": False,
            },
        ),
    )

    r = CliRunner().invoke(
        cli_mod.app,
        ["submit", "--project", "p", "--cwd", "/tmp", "--", "echo", "hi"],
    )
    assert r.exit_code == 0, r.output
    assert "Estimated wall p50 10m, p90 20m (n=8 prior runs)" in r.output


def test_submit_eta_banner_cold_start_line(monkeypatch):
    """ColdStart line printed when broker reports insufficient history."""
    import job_cli.cli as cli_mod

    captured: dict = {}
    monkeypatch.setattr(
        cli_mod,
        "_client",
        lambda: _FakePostClient(
            captured,
            response={"id": 43, "eta_basis": "insufficient-history-N=1"},
        ),
    )

    r = CliRunner().invoke(
        cli_mod.app,
        ["submit", "--project", "p", "--cwd", "/tmp", "--", "echo", "hi"],
    )
    assert r.exit_code == 0, r.output
    assert "ColdStart: insufficient history (n=1)" in r.output


def test_submit_no_eta_suppresses_banner(monkeypatch):
    """--no-eta suppresses the banner even when history is available."""
    import job_cli.cli as cli_mod

    captured: dict = {}
    monkeypatch.setattr(
        cli_mod,
        "_client",
        lambda: _FakePostClient(
            captured,
            response={
                "id": 44,
                "eta_p50_s": 600.0,
                "eta_p90_s": 1200.0,
                "eta_basis": "history-N=8",
            },
        ),
    )

    r = CliRunner().invoke(
        cli_mod.app,
        ["submit", "--project", "p", "--cwd", "/tmp", "--no-eta", "--", "echo", "hi"],
    )
    assert r.exit_code == 0, r.output
    assert "Estimated wall" not in r.output
    assert "ColdStart" not in r.output


def test_submit_eta_banner_appends_clipped_marker(monkeypatch):
    """Banner appends '⚠ history max-wall-clipped' when eta_clipped is True."""
    import job_cli.cli as cli_mod

    captured: dict = {}
    monkeypatch.setattr(
        cli_mod,
        "_client",
        lambda: _FakePostClient(
            captured,
            response={
                "id": 45,
                "eta_p50_s": 30.0,
                "eta_p90_s": 60.0,
                "eta_basis": "history-N=5",
                "eta_clipped": True,
            },
        ),
    )

    r = CliRunner().invoke(
        cli_mod.app,
        ["submit", "--project", "p", "--cwd", "/tmp", "--", "echo", "hi"],
    )
    assert r.exit_code == 0, r.output
    assert "history max-wall-clipped" in r.output


def test_submit_eta_banner_ctest_cost_data(monkeypatch):
    """Part 2: ctest-cost-K basis renders the ctest-cost-data banner."""
    import job_cli.cli as cli_mod

    captured: dict = {}
    monkeypatch.setattr(
        cli_mod,
        "_client",
        lambda: _FakePostClient(
            captured,
            response={
                "id": 47,
                "eta_p50_s": 125.5,
                "eta_p90_s": 125.5,
                "eta_basis": "ctest-cost-K=12",
            },
        ),
    )

    r = CliRunner().invoke(
        cli_mod.app,
        ["submit", "--project", "p", "--cwd", "/tmp", "--", "ctest", "-R", "Foo"],
    )
    assert r.exit_code == 0, r.output
    assert "ctest cost-data" in r.output
    assert "k=12 tests" in r.output


def test_submit_eta_banner_absent_when_no_basis(monkeypatch):
    """Defensive: no eta_basis field at all → no banner printed."""
    import job_cli.cli as cli_mod

    captured: dict = {}
    monkeypatch.setattr(cli_mod, "_client", lambda: _FakePostClient(captured, response={"id": 46}))

    r = CliRunner().invoke(
        cli_mod.app,
        ["submit", "--project", "p", "--cwd", "/tmp", "--", "echo", "hi"],
    )
    assert r.exit_code == 0, r.output
    assert "Estimated wall" not in r.output
    assert "ColdStart" not in r.output


def test_logs_prints_tail(monkeypatch):
    """`job logs <id>` hits /output and echoes the tail verbatim."""
    import job_cli.cli as cli_mod

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, path, params=None):
            assert path == "/jobs/7/output"
            assert params == {"tail": 8192}
            return _FakeResp(
                {
                    "tail": "hello world\n",
                    "size_bytes": 12,
                    "returned_bytes": 12,
                    "truncated": False,
                }
            )

    monkeypatch.setattr(cli_mod, "_client", lambda: FakeClient())
    r = CliRunner().invoke(cli_mod.app, ["logs", "7"])
    assert r.exit_code == 0
    assert "hello world" in r.stdout


def test_logs_no_capture_prints_marker(monkeypatch):
    """Empty log → friendly marker, not silent success."""
    import job_cli.cli as cli_mod

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, path, params=None):
            return _FakeResp({"tail": "", "size_bytes": 0, "returned_bytes": 0, "truncated": False})

    monkeypatch.setattr(cli_mod, "_client", lambda: FakeClient())
    r = CliRunner().invoke(cli_mod.app, ["logs", "7"])
    assert r.exit_code == 0
    assert "no log captured" in r.stdout


def test_list_renders_deps_markers(monkeypatch):
    """`job list` prints `deps: 10✓ 11⧖` under a child row."""
    import job_cli.cli as cli_mod

    jobs = [
        {
            "id": 10,
            "state": "completed",
            "project": "p",
            "cmd": ["true"],
            "depends_on": [],
        },
        {
            "id": 11,
            "state": "queued",
            "project": "p",
            "cmd": ["true"],
            "depends_on": [],
        },
        {
            "id": 12,
            "state": "queued",
            "project": "p",
            "cmd": ["true"],
            "depends_on": [10, 11],
        },
    ]

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, path, params=None):
            if path == "/workers":
                return _FakeResp(
                    [
                        {
                            "host": "w1",
                            "last_heartbeat": __import__("datetime")
                            .datetime.now(__import__("datetime").timezone.utc)
                            .isoformat(),
                            "state": "online",
                        }
                    ]
                )
            return _FakeResp(jobs)

    monkeypatch.setattr(cli_mod, "_client", lambda: FakeClient())
    r = CliRunner().invoke(cli_mod.app, ["list"])
    assert r.exit_code == 0, r.output
    assert "deps:" in r.stdout
    assert "10✓" in r.stdout
    assert "11⧖" in r.stdout


def test_delete_worker_success(monkeypatch):
    """`job delete-worker host` calls DELETE /workers/<host> and prints the result."""
    import job_cli.cli as cli_mod

    calls: list[tuple[str, str]] = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def delete_worker(self, host: str):
            calls.append(("delete_worker", host))
            return {"ok": True, "deleted": host}

    monkeypatch.setattr(cli_mod, "_client", lambda: FakeClient())
    r = CliRunner().invoke(cli_mod.app, ["delete-worker", "ghost"])
    assert r.exit_code == 0, r.output
    assert calls == [("delete_worker", "ghost")]
    assert "ghost" in r.stdout


def test_delete_worker_409_exits_nonzero(monkeypatch):
    """409 from the broker → exit 1 + refusal message on stderr."""
    import job_cli.cli as cli_mod
    from jobd.client import BrokerRefusal

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def delete_worker(self, host: str):
            raise BrokerRefusal("broker 409", status_code=409, detail=f"worker {host!r} is online")

    monkeypatch.setattr(cli_mod, "_client", lambda: FakeClient())
    r = CliRunner().invoke(cli_mod.app, ["delete-worker", "laptop"])
    assert r.exit_code == 1
    assert "is online" in r.output


def test_preempt_success(monkeypatch):
    """`job preempt 42` calls POST /jobs/42/preempt and prints the JobInfo."""
    import job_cli.cli as cli_mod

    calls: list[tuple[str, int]] = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def preempt(self, job_id: int):
            calls.append(("preempt", job_id))
            return {"id": job_id, "state": "running", "signal": "preempt"}

    monkeypatch.setattr(cli_mod, "_client", lambda: FakeClient())
    r = CliRunner().invoke(cli_mod.app, ["preempt", "42"])
    assert r.exit_code == 0, r.output
    assert calls == [("preempt", 42)]
    assert "preempt" in r.stdout


def test_preempt_409_exits_nonzero(monkeypatch):
    """Broker 409 (not preemptible / not running) → exit 1 with refusal message."""
    import job_cli.cli as cli_mod
    from jobd.client import BrokerRefusal

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def preempt(self, job_id: int):
            raise BrokerRefusal(
                "broker 409", status_code=409, detail=f"job {job_id} is not preemptible"
            )

    monkeypatch.setattr(cli_mod, "_client", lambda: FakeClient())
    r = CliRunner().invoke(cli_mod.app, ["preempt", "7"])
    assert r.exit_code == 1
    assert "not preemptible" in r.output


def _patch_ping_client(monkeypatch, *, response=None, raise_exc=None):
    """Install a fake JobdClient on cli_mod for `ping` tests."""
    import job_cli.cli as cli_mod

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, path, params=None):
            if raise_exc is not None:
                raise raise_exc
            return _FakeResp(response)

    monkeypatch.setattr(cli_mod, "JobdClient", FakeClient)
    return cli_mod


def test_ping_healthy_human(monkeypatch):
    """`job ping` against a healthy broker → exit 0, human output."""
    cli_mod = _patch_ping_client(monkeypatch, response={"status": "ok", "version": "0.1.0"})
    r = CliRunner().invoke(cli_mod.app, ["ping"])
    assert r.exit_code == 0, r.output
    assert "health:  ok" in r.stdout
    assert "version: 0.1.0" in r.stdout
    assert "latency:" in r.stdout


def test_ping_healthy_json(monkeypatch):
    """`job ping --json` returns a parseable JSON document with expected keys."""
    import json as _json

    cli_mod = _patch_ping_client(monkeypatch, response={"status": "ok", "version": "9.9.9"})
    r = CliRunner().invoke(cli_mod.app, ["ping", "--json"])
    assert r.exit_code == 0, r.output
    payload = _json.loads(r.stdout)
    assert payload["healthy"] is True
    assert payload["reachable"] is True
    assert payload["version"] == "9.9.9"
    assert payload["error"] is None
    assert isinstance(payload["latency_ms"], int)


def test_ping_unreachable_exits_2(monkeypatch):
    """Connection failure → exit 2 with error surfaced; JSON variant shape preserved."""
    import json as _json
    from jobd.client import BrokerUnreachable

    cli_mod = _patch_ping_client(
        monkeypatch, raise_exc=BrokerUnreachable("ConnectError: refused (JOBD_URL=http://x)")
    )
    r = CliRunner().invoke(cli_mod.app, ["ping", "--json"])
    assert r.exit_code == 2
    payload = _json.loads(r.stdout)
    assert payload["healthy"] is False
    assert payload["reachable"] is False
    assert payload["version"] is None
    assert "ConnectError" in payload["error"]


def test_ping_broker_returns_non_ok_exits_2(monkeypatch):
    """Reachable broker that returns a non-ok body → exit 2 (treats as unhealthy)."""
    cli_mod = _patch_ping_client(monkeypatch, response={"status": "degraded", "version": "0.1.0"})
    r = CliRunner().invoke(cli_mod.app, ["ping"])
    assert r.exit_code == 2
    assert "unreachable" in r.output  # human-text path uses "unreachable" for any non-ok
