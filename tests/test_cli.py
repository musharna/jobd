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
    import httpx
    import job_cli.cli as cli_mod

    captured = {}

    def fake_post(url, json, **kw):
        captured["url"] = url
        captured["body"] = json
        return httpx.Response(200, json={"id": 1})

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setattr(cli_mod, "BASE", "http://fake")

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
    import httpx
    import job_cli.cli as cli_mod

    captured = {}

    def fake_post(url, json, **kw):
        captured["body"] = json
        return httpx.Response(200, json={"id": 1})

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setattr(cli_mod, "BASE", "http://fake")

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
