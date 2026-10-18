"""The agent benchmark's two read-only guards, each beside its positive control.

``job_guard.py`` is the ``job`` the alt arm finds on PATH; ``readonly_proxy.py``
is the only way any arm reaches a broker. Every test asserts both directions in
one place: the mutation is refused AND a read-only call still goes through, so
a guard that refused everything (or a harness that never ran) cannot pass.

Nothing here touches a real broker: the upstream is a local fake that records
what reached it, and JOBD_URL / JOBD_API_TOKEN are removed from every child.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks" / "agent_qa"))

from job_guard import REFUSED_EXIT, write_shim  # noqa: E402
from readonly_proxy import ReadOnlyProxy  # noqa: E402

MUTATING = [
    ["cancel", "5"],
    ["preempt", "5"],
    ["preempt-blockers", "5"],
    ["adopt", "--pid", "123", "--project", "p"],
    ["delete-worker", "alpha"],
    ["projects", "set", "p", "90"],
    ["projects", "nudge", "p", "5"],
    ["fleet", "add", "host"],
    ["fleet", "status"],
    ["gpu-holders"],
    ["submit", "--project", "p", "--", "echo", "hi"],
    ["submit", "--project", "p", "--", "echo", "--explain"],  # after --: the command's
    ["--install-completion"],
    ["list", "--install-completion"],
    ["frobnicate"],
]


def _clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("JOBD_URL", "JOBD_API_TOKEN")}
    env.update(extra)
    return env


@pytest.fixture
def stub_job(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in for the real CLI that records each argv it is run with."""
    calls = tmp_path / "calls.jsonl"
    stub = tmp_path / "real-job"
    envs = tmp_path / "envs.jsonl"
    stub.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"open({str(envs)!r}, 'a').write(json.dumps({{k: os.environ.get(k) for k in "
        f"('JOBD_URL', 'JOBD_API_TOKEN')}}) + '\\n')\n"
    )
    stub.chmod(0o755)
    return stub, calls


def _run_shim(shim: Path, argv: list[str], real: list[str], **env: str):
    env.setdefault("JOBD_BENCH_PROXY_URL", "http://127.0.0.1:9")
    return subprocess.run(
        [str(shim), *argv],
        env=_clean_env(JOBD_BENCH_REAL_JOB=json.dumps(real), **env),
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_guard_refuses_every_mutating_subcommand_and_passes_reads(tmp_path, stub_job) -> None:
    stub, calls = stub_job
    shim = write_shim(tmp_path / "bin")
    for argv in MUTATING:
        r = _run_shim(shim, argv, [str(stub)])
        assert r.returncode == REFUSED_EXIT, (argv, r.returncode, r.stderr)
        assert "refused by the benchmark's read-only guard" in r.stderr, argv
    assert not calls.exists(), f"the real CLI ran for a refused command: {calls.read_text()}"
    # Positive control, same shim, same stub: read-only commands reach it unchanged.
    for argv in (["list", "--state", "failed"], ["status", "42"], ["logs", "42"],
                 ["workers"], ["projects", "list"], ["--version"]):  # fmt: skip
        r = _run_shim(shim, argv, [str(stub)])
        assert r.returncode == 0, (argv, r.stderr)
    seen = [json.loads(ln) for ln in calls.read_text().splitlines()]
    assert seen == [["list", "--state", "failed"], ["status", "42"], ["logs", "42"],
                    ["workers"], ["projects", "list"], ["--version"]]  # fmt: skip


def test_the_real_cli_gets_the_proxy_url_and_no_token_whatever_the_shell_exported(
    tmp_path, stub_job
) -> None:
    """Seen in the pilot: the Bash tool's zsh sourced ~/.zshenv, which put the
    live broker's URL and token back, and `job` went around the proxy."""
    stub, calls = stub_job
    shim = write_shim(tmp_path / "bin")
    shell_exported = {"JOBD_URL": "http://live-broker.invalid:8765", "JOBD_API_TOKEN": "live"}
    r = _run_shim(shim, ["workers"], [str(stub)], JOBD_BENCH_PROXY_URL="http://127.0.0.1:4242",
                  **shell_exported)  # fmt: skip
    assert r.returncode == 0, r.stderr
    env = json.loads((tmp_path / "envs.jsonl").read_text())
    assert env == {"JOBD_URL": "http://127.0.0.1:4242", "JOBD_API_TOKEN": None}
    # Without a proxy to point at, the guard runs nothing, even a read.
    r = _run_shim(shim, ["workers"], [str(stub)], JOBD_BENCH_PROXY_URL="", **shell_exported)
    assert r.returncode == REFUSED_EXIT and "no JOBD_BENCH_PROXY_URL" in r.stderr
    assert len(calls.read_text().splitlines()) == 1


def test_submit_runs_only_as_explain_with_the_flag_first(tmp_path, stub_job) -> None:
    """--explain is re-inserted right after `submit`, where no option can take
    it as a value and no `--` can hide it."""
    stub, calls = stub_job
    shim = write_shim(tmp_path / "bin")
    r = _run_shim(shim, ["submit", "--project", "p", "--explain", "--", "echo"], [str(stub)])
    assert r.returncode == 0, r.stderr
    assert json.loads(calls.read_text()) == [
        "submit", "--explain", "--project", "p", "--explain", "--", "echo"
    ]  # fmt: skip


class _Upstream:
    """A fake broker: records every request, answers 200 with a JSON body."""

    def __init__(self) -> None:
        self.seen: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a) -> None:
                pass

            def _any(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                outer.seen.append({
                    "method": self.command, "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "body": self.rfile.read(n).decode() if n else "",
                })  # fmt: skip
                body = b"[]" if self.path.startswith(("/jobs", "/workers")) else b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-Total-Count", "0")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_DELETE = do_PUT = _any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def upstream() -> Iterator[_Upstream]:
    u = _Upstream()
    yield u
    u.server.shutdown()
    u.server.server_close()


def test_the_proxy_refuses_mutations_and_forwards_reads_with_its_own_token(
    upstream, tmp_path
) -> None:
    log = tmp_path / "proxy.jsonl"
    with ReadOnlyProxy(upstream.url, "secret", log=log) as p, httpx.Client(base_url=p.url) as c:
        refused = [
            c.post("/jobs/5/cancel", json={}),
            c.post("/jobs/5/preempt"),
            c.post("/jobs/5/preempt-blockers"),
            c.delete("/workers/alpha"),
            c.post("/submit", json={"cmd": ["x"], "cwd": "/tmp", "project": "p"}),
            c.post("/submit", json={"cmd": ["x"], "dry_run": False}),
            c.post("/submit", json={"cmd": ["x"], "dry_run": "true"}),  # a string is not true
            c.post("/submit", content=b"not json"),
            c.post("/adopt", json={}),
            c.post("/heartbeat", json={}),
            c.post("/next-job", json={}),
            c.post("/events", json={}),
            c.post("/projects/p", json={"priority": 90}),
            c.post("/reload"),
            c.get("/gpu-holders"),
        ]
        assert [r.status_code for r in refused] == [403] * len(refused)
        assert upstream.seen == [], f"a refused request reached the broker: {upstream.seen}"
        # Positive control through the same proxy instance.
        ok = [
            c.get("/jobs", params={"limit": 1}, headers={"Authorization": "Bearer agent"}),
            c.get("/workers"),
            c.post("/submit", json={"cmd": ["x"], "cwd": "/tmp", "project": "p", "dry_run": True}),
            c.post("/resolve", json={"cmd": ["x"], "cwd": "/tmp", "project": "p"}),
        ]
    assert [r.status_code for r in ok] == [200] * len(ok)
    assert [(s["method"], s["path"].split("?")[0]) for s in upstream.seen] == [
        ("GET", "/jobs"), ("GET", "/workers"), ("POST", "/submit"), ("POST", "/resolve")
    ]  # fmt: skip
    # The proxy's token, never the caller's.
    assert {s["auth"] for s in upstream.seen} == {"Bearer secret"}
    decisions = [json.loads(ln) for ln in log.read_text().splitlines()]
    assert sum(not d["allowed"] for d in decisions) == len(refused)
    assert sum(d["allowed"] for d in decisions) == len(ok)


def test_the_real_cli_is_stopped_by_the_guard_and_again_by_the_proxy(upstream, tmp_path) -> None:
    """Real execution: the repo's own `job` CLI, through the shim, through the
    proxy, to the fake broker. With the guard bypassed, the proxy still stops
    the cancel; a read-only `job list` reaches the broker both ways."""
    real_job = Path(sys.executable).parent / "job"
    if not real_job.exists():
        pytest.skip(f"no `job` entry point beside {sys.executable}")
    shim = write_shim(tmp_path / "bin")
    dead = "http://127.0.0.1:9"  # what a shell startup file might export instead
    with ReadOnlyProxy(upstream.url, "secret") as p:
        r = _run_shim(shim, ["cancel", "5"], [str(real_job)], JOBD_URL=dead,
                      JOBD_BENCH_PROXY_URL=p.url)  # fmt: skip
        assert r.returncode == REFUSED_EXIT, r.stderr
        bypass = subprocess.run(
            [str(real_job), "cancel", "5"], env=_clean_env(JOBD_URL=p.url),
            capture_output=True, text=True, timeout=60,
        )  # fmt: skip
        assert bypass.returncode != 0, bypass.stdout
        assert upstream.seen == [], upstream.seen
        r = _run_shim(shim, ["list", "--limit", "1"], [str(real_job)], JOBD_URL=dead,
                      JOBD_API_TOKEN="live", JOBD_BENCH_PROXY_URL=p.url)  # fmt: skip
        assert r.returncode == 0, r.stderr
    reached = [(s["method"], s["path"].split("?")[0]) for s in upstream.seen]
    assert ("GET", "/jobs") in reached, reached
    assert {m for m, _ in reached} == {"GET"}, reached
