"""`job fleet` — real-execution tests in the CD-suite style.

The ssh stub records argv and stdin, so the tests prove the properties that
matter: the token NEVER rides an argv (local or remote — it travels only
inside stdin heredocs), the installer and updater sources actually go over the
wire, the generated units can't drift from the committed templates'
load-bearing directives, and the wheel really carries the fleet assets
(a force-include that silently drops would strand `fleet add` at runtime —
so we build a real wheel and look inside).
"""

from __future__ import annotations

import re
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest
import respx
from httpx import Response
from typer.testing import CliRunner

from job_cli.cli import app
from job_cli.fleet import _UPDATE_SERVICE, _UPDATE_TIMER, _WORKER_UNIT

_REPO_ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()

_TOKEN = "sekrit-fleet-token-123"
_BROKER = "http://127.0.0.1:9999"


@pytest.fixture
def ssh_stub(tmp_path, monkeypatch):
    """A fake `ssh` first on PATH: records argv + stdin, exits 0."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_file = tmp_path / "ssh-argv"
    stdin_file = tmp_path / "ssh-stdin"
    stub = bin_dir / "ssh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" > {argv_file}\n'
        f"cat > {stdin_file}\n"
        'echo "fleet-add: bootstrap complete"\n'
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
    # cli.BASE is bound at import time from JOBD_URL — patch the attribute,
    # not just the env; the token IS read per-JobdClient-construction.
    import job_cli.cli as cli_mod

    monkeypatch.setattr(cli_mod, "BASE", _BROKER)
    monkeypatch.setenv("JOBD_API_TOKEN", _TOKEN)
    return argv_file, stdin_file


def _mock_broker(mock, *, worker_registered: bool):
    mock.get(f"{_BROKER}/health").mock(
        return_value=Response(200, json={"status": "ok", "version": "0.5.28"})
    )
    workers = (
        [
            {
                "host": "gpubox",
                "version": "0.5.28",
                "state": "online",
                "running": 0,
                "max_concurrent": 1,
            }
        ]
        if worker_registered
        else []
    )
    mock.get(f"{_BROKER}/workers").mock(return_value=Response(200, json=workers))


@respx.mock
def test_fleet_add_pushes_everything_over_stdin_and_verifies(ssh_stub):
    argv_file, stdin_file = ssh_stub
    _mock_broker(respx, worker_registered=True)

    r = runner.invoke(app, ["fleet", "add", "me@gpubox"])
    assert r.exit_code == 0, r.output
    assert "pinned to broker" in r.output and "0.5.28" in r.output
    assert "gpubox registered" in r.output

    argv = argv_file.read_text().splitlines()
    stdin = stdin_file.read_text()

    # THE security property: the token is on no argv, only inside stdin.
    assert all(_TOKEN not in a for a in argv), argv
    assert _TOKEN in stdin
    assert f"JOBD_API_TOKEN={_TOKEN}" in stdin

    # ssh went to the right place and ran bash -s (script on stdin).
    assert argv[0] == "me@gpubox" and argv[1] == "bash -s"

    # Both shell assets really went over the wire, byte-exact.
    assert "install-worker.sh — set up a jobd worker on a fresh host." in stdin
    assert "VENV=${JOBD_WORKER_VENV:-$HOME/jobd-worker/.venv}" in stdin

    # Installer runs pinned to the broker's version; env file is 600.
    assert "--broker http://127.0.0.1:9999 --host gpubox --version 0.5.28" in stdin
    assert "chmod 600 ~/.config/jobd/worker.env" in stdin

    # Units land and both are enabled now.
    assert "systemctl --user enable --now job-worker.service" in stdin
    assert "systemctl --user enable --now jobd-worker-update.timer" in stdin


@respx.mock
def test_fleet_add_reports_when_worker_never_registers(ssh_stub):
    _mock_broker(respx, worker_registered=False)
    import job_cli.fleet as fleet_mod

    # Shrink the 60s registration window: the deadline math uses monotonic;
    # patch time in the fleet module to a fast-forwarding clock.
    real_monotonic = fleet_mod.time.monotonic
    t0 = real_monotonic()

    class _FastTime:
        @staticmethod
        def monotonic():
            return real_monotonic() + (real_monotonic() - t0) * 1000

        @staticmethod
        def sleep(_s):
            pass

    fleet_mod.time = _FastTime()
    try:
        r = runner.invoke(app, ["fleet", "add", "me@gpubox"])
    finally:
        import time as _time

        fleet_mod.time = _time
    assert r.exit_code == 1
    assert "did not register" in r.output
    assert "systemctl --user status job-worker.service" in r.output


@respx.mock
def test_fleet_add_dry_run_changes_nothing(ssh_stub):
    argv_file, stdin_file = ssh_stub
    _mock_broker(respx, worker_registered=False)

    r = runner.invoke(app, ["fleet", "add", "me@gpubox", "--dry-run"])
    assert r.exit_code == 0, r.output
    stdin = stdin_file.read_text()
    assert "--dry-run" in stdin
    # No secrets, no unit writes, no enables in a dry run. (The installer
    # SOURCE mentions systemctl in its echoed instructions — assert on
    # actual command lines, which start unindented.)
    assert _TOKEN not in stdin
    assert "\nsystemctl --user enable" not in stdin
    assert "worker.env" not in stdin


@respx.mock
def test_fleet_add_no_systemd_skips_units(ssh_stub):
    argv_file, stdin_file = ssh_stub
    _mock_broker(respx, worker_registered=False)

    r = runner.invoke(app, ["fleet", "add", "me@gpubox", "--no-systemd"])
    assert r.exit_code == 0, r.output
    stdin = stdin_file.read_text()
    assert "\nsystemctl --user enable" not in stdin
    assert "job-worker.service <<" not in stdin  # no unit heredocs written
    # Venv + env file still land (manual start possible).
    assert "install-worker.sh" in stdin and "worker.env" in stdin
    assert "Start manually" in r.output


@respx.mock
def test_fleet_status_shows_drift(ssh_stub, monkeypatch):
    respx.get(f"{_BROKER}/health").mock(
        return_value=Response(200, json={"status": "ok", "version": "0.5.28"})
    )
    respx.get(f"{_BROKER}/workers").mock(
        return_value=Response(
            200,
            json=[
                {
                    "host": "fresh",
                    "version": "0.5.28",
                    "state": "online",
                    "running": 0,
                    "max_concurrent": 1,
                },
                {
                    "host": "stale",
                    "version": "0.5.25",
                    "state": "online",
                    "running": 1,
                    "max_concurrent": 1,
                },
            ],
        )
    )
    r = runner.invoke(app, ["fleet", "status"])
    assert r.exit_code == 0, r.output
    assert "broker: 0.5.28" in r.output
    fresh_line = next(line for line in r.output.splitlines() if line.startswith("fresh:"))
    stale_line = next(line for line in r.output.splitlines() if line.startswith("stale:"))
    assert "behind broker" not in fresh_line
    assert "behind broker" in stale_line and "running=1/1" in stale_line


# --- drift guards -------------------------------------------------------------


def _directives(unit_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in unit_text.splitlines():
        m = re.match(r"^([A-Za-z]+)=(.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


def test_generated_worker_unit_matches_template_load_bearing_directives():
    """The generated unit and scripts/job-worker.service must agree on the
    directives with incident history behind them: KillMode=mixed (drain race),
    TimeoutStopSec (drain budget), Restart policy. Placeholders (ExecStart,
    env delivery) legitimately differ — the generated unit uses the shared
    env file instead of EDIT-me inline values."""
    template = _directives((_REPO_ROOT / "scripts" / "job-worker.service").read_text())
    generated = _directives(_WORKER_UNIT)
    for key in ("KillMode", "TimeoutStopSec", "Restart", "RestartSec", "Type", "WantedBy"):
        assert generated[key] == template[key], (
            f"generated job-worker.service diverges from the committed template on "
            f"{key}: {generated[key]!r} != {template[key]!r}"
        )
    assert generated["ExecStart"].endswith("/jobd-worker")
    assert generated["EnvironmentFile"] == "%h/.config/jobd/worker.env"


def test_generated_update_units_point_at_pushed_updater():
    """The committed update unit references %h/jobd/scripts/... — a git-clone
    path that does NOT exist on a fleet-added host. The generated units must
    point at the pushed copy and share the single env file."""
    svc = _directives(_UPDATE_SERVICE)
    assert svc["ExecStart"] == "%h/jobd-worker/update-worker.sh"
    assert svc["EnvironmentFile"] == "%h/.config/jobd/worker.env"
    assert svc["Type"] == "oneshot"
    timer = _directives(_UPDATE_TIMER)
    template_timer = _directives((_REPO_ROOT / "scripts" / "jobd-worker-update.timer").read_text())
    for key in ("OnBootSec", "OnUnitActiveSec", "AccuracySec", "RandomizedDelaySec"):
        assert timer[key] == template_timer[key]


def test_wheel_carries_fleet_assets(tmp_path):
    """Build the real wheel and look inside: a force-include that silently
    drops (renamed script, pyproject typo) would strand `fleet add` on every
    pip-installed CLI while the editable-install fallback kept tests green."""
    subprocess.run(
        ["uv", "build", "--wheel", "-o", str(tmp_path)],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
    )
    wheel = next(tmp_path.glob("jobd-*.whl"))
    names = zipfile.ZipFile(wheel).namelist()
    for asset in ("jobd/fleet_assets/install-worker.sh", "jobd/fleet_assets/update-worker.sh"):
        assert asset in names, f"{asset} missing from the wheel — force-include broken"
