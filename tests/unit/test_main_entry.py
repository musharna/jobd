"""--help / --version short-circuit before DB init.

Regression: `jobd --help` previously crashed with SQLAlchemy
OperationalError because run() ignored sys.argv and called build_app()
unconditionally, which opens the SQLite file at JOBD_DB_URL.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from jobd import main as main_mod


def test_parse_args_help_exits_zero():
    with pytest.raises(SystemExit) as ei:
        main_mod._parse_args(["--help"])
    assert ei.value.code == 0


def test_parse_args_version_exits_zero():
    with pytest.raises(SystemExit) as ei:
        main_mod._parse_args(["--version"])
    assert ei.value.code == 0


def test_parse_args_unknown_flag_exits_nonzero():
    with pytest.raises(SystemExit) as ei:
        main_mod._parse_args(["--no-such-flag"])
    assert ei.value.code != 0


def test_help_subprocess_no_sqlite_error():
    """Real-execution check: invoking the installed `jobd` entry point with
    --help in a fresh process prints usage on stdout and does NOT touch
    the SQLite path. Drives the real argparse + module-import chain that
    the unit-level _parse_args tests cannot."""
    r = subprocess.run(
        [sys.executable, "-c", "from jobd.main import run; run()", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        env={"PATH": "/usr/bin:/bin", "JOBD_DB_URL": "sqlite:////nonexistent/dir/x.db"},
    )
    assert r.returncode == 0, f"exit={r.returncode} stderr={r.stderr}"
    assert "usage:" in r.stdout
    assert "OperationalError" not in r.stderr
    assert "sqlite" not in r.stderr.lower()
