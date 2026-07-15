"""Real-EXECUTION coverage for the CD scripts (audit 2026-07-15).

The deploy-lint tests read these scripts as text; `bash -n` proves they parse.
Neither proves they RUN — and this pipeline's history is exactly that failure:
the worker updater crashlooped on every host (exit 127) twice, green the whole
time, because no guard ever engaged a real environment. healthcheck.py's
predecessor "reported healthy for weeks" against the wrong daemon.

So: run them. deploy-broker.sh runs against a stubbed `docker`/`curl` PATH —
including the ROLLBACK path, which otherwise first executes for real during a
failed production deploy, the one moment it must not be wrong. healthcheck.py
runs against live sockets serving good, alien, and absent payloads.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEPLOY = _REPO_ROOT / "scripts" / "deploy-broker.sh"
_UPDATE = _REPO_ROOT / "scripts" / "update-worker.sh"
_HEALTHCHECK = _REPO_ROOT / "scripts" / "healthcheck.py"


# --- harness ----------------------------------------------------------------


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def cd_env(tmp_path):
    """A fake broker host: stub bin/ on PATH, a .env, and a 'running version'
    file the stub curl serves and the stub docker bumps on `compose up`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    version_file = tmp_path / "running-version"
    version_file.write_text("0.5.16")
    docker_log = tmp_path / "docker.log"
    jobs_dir = tmp_path / "jobs-fixtures"
    jobs_dir.mkdir()

    # curl: answer by URL (last arg), like the broker would. Flags are ignored —
    # both scripts put the URL last. -K <(...) config files are skipped the same way.
    _write_exec(
        bin_dir / "curl",
        f"""#!/usr/bin/env bash
url="${{@: -1}}"
case "$url" in
  */health) printf '{{"status":"ok","version":"%s"}}' "$(cat {version_file})" ;;
  */jobs*state_filter=assigned*) cat "{jobs_dir}/assigned.json" 2>/dev/null || echo '[]' ;;
  */jobs*state_filter=running*)  cat "{jobs_dir}/running.json"  2>/dev/null || echo '[]' ;;
  *) echo '{{}}' ;;
esac
""",
    )
    # docker: log every invocation; `compose up` brings up the "new" container,
    # i.e. flips the served version to $FAKE_NEW_VERSION when set (leave it unset
    # to simulate an image that never comes up healthy).
    _write_exec(
        bin_dir / "docker",
        f"""#!/usr/bin/env bash
echo "docker $*" >> "{docker_log}"
if [ "$1" = "compose" ] && [ "$2" = "up" ] && [ -n "${{FAKE_NEW_VERSION:-}}" ]; then
  printf '%s' "$FAKE_NEW_VERSION" > "{version_file}"
fi
exit 0
""",
    )
    # sleep: the health-gate/rollback paths sleep 3s/5s between probes; a stub
    # keeps the failure tests fast without touching the script's logic.
    _write_exec(bin_dir / "sleep", "#!/usr/bin/env bash\nexit 0\n")

    env_file = tmp_path / ".env"
    env_file.write_text('JOBD_HOST=127.0.0.1\nJOBD_API_TOKEN="sekrit"\nJOBD_TAG=0.5.16\n')

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["JOBD_DIR"] = str(tmp_path)
    env["METRICS_FILE"] = str(tmp_path / "metrics.prom")
    env["HEALTH_TIMEOUT_S"] = "2"
    return tmp_path, env, version_file, docker_log, jobs_dir


def _run(script: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script), *args], env=env, capture_output=True, text=True, timeout=60
    )


# --- deploy-broker.sh --------------------------------------------------------


def test_deploy_happy_path_pins_pulls_restarts_and_gates(cd_env):
    tmp_path, env, version_file, docker_log, _ = cd_env
    env["FAKE_NEW_VERSION"] = "0.5.17"

    r = _run(_DEPLOY, env, "0.5.17")

    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    log = docker_log.read_text().splitlines()
    assert any("pull ghcr.io/musharna/jobd:0.5.17" in ln for ln in log), (
        f"no pull before restart: {log}"
    )
    assert log.index(next(ln for ln in log if "pull" in ln)) < log.index(
        next(ln for ln in log if "compose up" in ln)
    ), "the image must be pulled BEFORE the restart — a failed pull must not take the broker down"
    assert "JOBD_TAG=0.5.17" in (tmp_path / ".env").read_text(), "the pin was not written"
    assert "jobd_deploy_last_run_success 1" in (tmp_path / "metrics.prom").read_text()
    mode = stat.S_IMODE((tmp_path / ".env").stat().st_mode)
    assert mode == 0o600, f".env holds the API token and must be 0600, got {oct(mode)}"


def test_deploy_health_gate_failure_rolls_back_the_pin(cd_env):
    """THE payoff path. If the new image never reports its version, the deploy
    must restore the previous pin, restart onto it, and exit nonzero — this
    logic otherwise first runs for real during a failed production deploy."""
    tmp_path, env, version_file, docker_log, _ = cd_env
    # FAKE_NEW_VERSION unset: `compose up` succeeds but the broker keeps serving 0.5.16.

    r = _run(_DEPLOY, env, "0.5.17")

    assert r.returncode != 0, "a deploy whose health gate never passed exited 0"
    assert "HEALTH GATE FAILED" in r.stdout
    assert "JOBD_TAG=0.5.16" in (tmp_path / ".env").read_text(), (
        f"the pin was not rolled back:\n{(tmp_path / '.env').read_text()}"
    )
    ups = [ln for ln in docker_log.read_text().splitlines() if "compose up" in ln]
    assert len(ups) == 2, f"expected deploy + rollback compose up, got: {ups}"
    assert "jobd_deploy_last_run_success 0" in (tmp_path / "metrics.prom").read_text()


@pytest.mark.parametrize("evil", ['0.5.17"; rm -rf /', "0.5.17|x", "latest", "v0.5.17"])
def test_deploy_refuses_a_target_that_is_not_a_plain_version(cd_env, evil):
    """TARGET arrives from argv or a remote API and is sed'd into the file that
    holds the API token. Anything but X.Y.Z must die before touching state."""
    tmp_path, env, _, docker_log, _ = cd_env
    before = (tmp_path / ".env").read_text()

    r = _run(_DEPLOY, env, evil)

    assert r.returncode != 0, f"target {evil!r} was accepted"
    assert (tmp_path / ".env").read_text() == before, ".env was modified by a refused target"
    assert not docker_log.exists(), "docker ran for a refused target"


def test_deploy_dry_run_changes_nothing(cd_env):
    tmp_path, env, _, docker_log, _ = cd_env
    env["DRY_RUN"] = "1"
    before = (tmp_path / ".env").read_text()

    r = _run(_DEPLOY, env, "0.5.17")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "DRY_RUN" in r.stdout
    assert (tmp_path / ".env").read_text() == before
    assert not docker_log.exists(), "DRY_RUN ran docker"


# --- update-worker.sh --------------------------------------------------------


def _worker_env(cd_env, tmp_path_factory=None):
    tmp_path, env, version_file, docker_log, jobs_dir = cd_env
    env["JOBD_URL"] = "http://127.0.0.1:1"  # stub curl answers by URL suffix
    env["JOBD_API_TOKEN"] = "sekrit"
    env["JOBD_WORKER_HOST"] = "testhost"
    env["JOBD_WORKER_VENV"] = str(tmp_path / "no-such-venv")  # installed=none
    env["DRY_RUN"] = "1"
    return tmp_path, env, version_file, jobs_dir


def test_update_worker_dry_run_reports_the_pinned_target(cd_env):
    tmp_path, env, version_file, jobs_dir = _worker_env(cd_env)
    version_file.write_text("0.5.99")

    r = _run(_UPDATE, env)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "jobd[worker]==0.5.99" in r.stdout, (
        f"the dry-run did not pin the broker's version:\n{r.stdout}"
    )


def test_update_worker_defers_when_a_job_row_exists(cd_env):
    """The gate counts assigned+running JOB ROWS owned by this host — the
    authoritative signal, written synchronously at claim time (F2)."""
    tmp_path, env, version_file, jobs_dir = _worker_env(cd_env)
    version_file.write_text("0.5.99")
    (jobs_dir / "running.json").write_text(
        json.dumps([{"id": 1, "worker": "testhost", "state": "running"}])
    )

    r = _run(_UPDATE, env)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "deferring" in r.stdout, f"a busy worker was not deferred:\n{r.stdout}\n{r.stderr}"
    assert "would install" not in r.stdout


def test_update_worker_ignores_other_hosts_jobs(cd_env):
    tmp_path, env, version_file, jobs_dir = _worker_env(cd_env)
    version_file.write_text("0.5.99")
    (jobs_dir / "running.json").write_text(
        json.dumps([{"id": 1, "worker": "some-other-host", "state": "running"}])
    )

    r = _run(_UPDATE, env)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "would install" in r.stdout, (
        f"another host's job deferred OUR upgrade:\n{r.stdout}\n{r.stderr}"
    )


def test_update_worker_prints_the_diagnostic_when_the_broker_is_unreachable(cd_env):
    """Under `set -e` a failed $(api|python3) assignment used to kill the script
    before its own error message — the fail-safe branch was unreachable. Prove
    the written diagnostic actually prints (audit 2026-07-15 L-2)."""
    tmp_path, env, version_file, jobs_dir = _worker_env(cd_env)
    bin_dir = tmp_path / "bin"
    _write_exec(bin_dir / "curl", "#!/usr/bin/env bash\nexit 22\n")  # broker down

    r = _run(_UPDATE, env)

    assert r.returncode == 1
    assert "could not read the broker version" in r.stderr, (
        f"the unreachable-broker diagnostic never printed:\nstdout:{r.stdout}\nstderr:{r.stderr}"
    )


# --- healthcheck.py ----------------------------------------------------------


def _serve(payload: bytes, status: int = 200) -> tuple[HTTPServer, int]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):  # keep pytest output clean
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _probe(port: int) -> subprocess.CompletedProcess:
    import sys

    env = os.environ.copy()
    env["JOBD_HOST"] = "127.0.0.1"
    env["JOBD_PORT"] = str(port)
    env["JOBD_API_TOKEN"] = "tok"
    return subprocess.run(
        [sys.executable, str(_HEALTHCHECK)], env=env, capture_output=True, text=True, timeout=30
    )


def test_healthcheck_passes_on_a_real_jobd_payload():
    server, port = _serve(b'{"status": "ok", "version": "0.5.25"}')
    try:
        r = _probe(port)
        assert r.returncode == 0, r.stderr
    finally:
        server.shutdown()


def test_healthcheck_fails_on_an_alien_daemon_on_the_right_port():
    """The exact historic failure: something ELSE answers the socket. A bare TCP
    probe passed for weeks; the body check must not."""
    server, port = _serve(b'{"hello": "i am not jobd"}')
    try:
        r = _probe(port)
        assert r.returncode != 0, "an alien /health payload was called healthy"
        assert "not jobd" in r.stderr or "payload" in r.stderr
    finally:
        server.shutdown()


def test_healthcheck_fails_on_http_error():
    server, port = _serve(b"nope", status=404)
    try:
        r = _probe(port)
        assert r.returncode != 0, "a 404 was called healthy"
    finally:
        server.shutdown()


def test_healthcheck_fails_when_nothing_listens():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # free the port: connection refused
    r = _probe(port)
    assert r.returncode != 0, "a dead socket was called healthy"
