"""The `job` console entry point (`cli.main`) wraps the Typer app in a broker-
error boundary: a down / erroring / refusing broker yields a one-line stderr
diagnostic and a clean exit code, never a raw traceback (audit 2026-07-01, LOW —
most CLI commands previously tracebacked on BrokerUnreachable/ServerError/
Refusal; only a handful handled them). The boundary lives in `main()`, not
`app()`, so these tests drive `main()` directly (CliRunner invokes `app`).
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import job_cli.cli as cli_mod
from jobd.client import BrokerRefusal, BrokerServerError, BrokerUnreachable


def _run_main_raising(exc, monkeypatch):
    """Replace the Typer `app` with a stub that raises `exc`, run `main()`, and
    return the SystemExit code the boundary produced."""

    def _boom():
        raise exc

    monkeypatch.setattr(cli_mod, "app", _boom)
    with pytest.raises(SystemExit) as ei:
        cli_mod.main()
    return ei.value.code


def test_main_unreachable_exits_2_clean(monkeypatch, capsys):
    code = _run_main_raising(BrokerUnreachable("ConnectError: connection refused"), monkeypatch)
    assert code == 2
    err = capsys.readouterr().err
    assert "unreachable" in err.lower()
    assert "job ping" in err  # points the user at the diagnostic command
    assert "Traceback" not in err


def test_main_server_error_exits_2_clean(monkeypatch, capsys):
    code = _run_main_raising(BrokerServerError("boom", status_code=503), monkeypatch)
    assert code == 2
    err = capsys.readouterr().err
    assert "503" in err
    assert "Traceback" not in err


def test_main_refusal_exits_1_clean(monkeypatch, capsys):
    code = _run_main_raising(
        BrokerRefusal("no", status_code=409, detail="worker still online"),
        monkeypatch,
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "409" in err
    assert "worker still online" in err
    assert "Traceback" not in err


def test_main_passes_normal_exit_through(monkeypatch):
    """A normal command completion (SystemExit(0)) and a typer.Exit are NOT
    intercepted by the broker boundary — only broker exceptions are."""

    def _ok():
        raise SystemExit(0)

    monkeypatch.setattr(cli_mod, "app", _ok)
    with pytest.raises(SystemExit) as ei:
        cli_mod.main()
    assert ei.value.code == 0


def test_real_entrypoint_unreachable_broker_no_traceback():
    """Real-execution check: the actual `job` entry point (`cli.main`, the
    console-script target) run against a dead broker port prints a clean
    diagnostic and exits 2 — no traceback. Drives the real argparse/httpx/
    client-wrapping chain the unit tests stub out. Port 45789 is unbound on
    loopback, so the connect is refused immediately."""
    r = subprocess.run(
        [sys.executable, "-c", "from job_cli.cli import main; main()", "workers"],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", "JOBD_URL": "http://127.0.0.1:45789"},
    )
    assert r.returncode == 2, f"exit={r.returncode} stderr={r.stderr}"
    assert "unreachable" in r.stderr.lower()
    assert "Traceback" not in r.stderr
