"""``jobd-worker`` and ``jobd-mcp`` load on a base install and name the extra.

``pip install jobd`` installs all four console scripts, but the worker's and the
MCP server's modules import packages only the ``worker`` and ``mcp`` extras
bring. Pointed straight at those modules, the two commands died with a bare
``ModuleNotFoundError`` traceback (found by the wheel check on 2026-11-14: a
base install could not load either script). They now point at ``jobd._entry``,
which imports nothing optional.

The subprocess tests block a module the way a missing install does
(``sys.modules[name] = None``) in a fresh interpreter, so the worker module is
really imported there and nothing cached in this process can stand in for it.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from jobd import _entry

ROOT = Path(__file__).resolve().parents[1]


def _python(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize(
    ("func", "command", "extra", "blocked", "module"),
    [
        ("worker", "jobd-worker", "worker", "psutil", "jobd.worker.job_worker"),
        ("mcp", "jobd-mcp", "mcp", "jsonschema", "jobd.mcp.server"),
    ],
)
def test_a_command_without_its_extra_says_which_extra_to_install(
    func: str, command: str, extra: str, blocked: str, module: str
) -> None:
    missing = _python(
        f"import sys; sys.modules[{blocked!r}] = None\nfrom jobd import _entry; _entry.{func}()"
    )
    assert missing.returncode == 1, missing.stderr
    assert f"{command} needs the '{extra}' extra" in missing.stderr
    assert f"missing module: {blocked}" in missing.stderr
    assert f"pip install 'jobd[{extra}]'" in missing.stderr
    assert "Traceback" not in missing.stderr

    # Positive control: with the extra present, the same lookup returns the
    # real main. It is not called: the worker would start, the server would
    # sit on stdin.
    present = _python(
        "from jobd import _entry\n"
        f"main = _entry._resolve({command!r}, {extra!r}, {module!r}, ({blocked!r},))\n"
        "print(main.__module__, main.__name__)"
    )
    assert present.returncode == 0, present.stderr
    assert present.stdout.split() == [module, "main"]


def test_a_missing_module_the_extra_does_not_bring_keeps_its_traceback() -> None:
    """A broken install or a bug inside jobd must not read as "install the extra"."""
    with pytest.raises(ModuleNotFoundError) as outcome:
        _entry._resolve("jobd-worker", "worker", "jobd._no_such_module", _entry._WORKER_NEEDS)
    assert outcome.value.name == "jobd._no_such_module"
    # Positive control: the same call with a module that exists returns its main.
    assert _entry._resolve("job", "worker", "job_cli.cli", _entry._WORKER_NEEDS).__name__ == "main"


def test_the_extra_message_is_raised_only_for_a_module_the_extra_brings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "needs_absent_dep.py").write_text("import absent_dep_for_this_test\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(SystemExit) as named:
        _entry._resolve("cmd", "worker", "needs_absent_dep", ("absent_dep_for_this_test",))
    assert "cmd needs the 'worker' extra" in str(named.value)
    assert "pip install 'jobd[worker]'" in str(named.value)
    monkeypatch.delitem(sys.modules, "needs_absent_dep", raising=False)
    with pytest.raises(ModuleNotFoundError):
        _entry._resolve("cmd", "worker", "needs_absent_dep", ("some_other_dep",))


def test_a_submodule_missing_from_an_installed_package_keeps_its_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The package is there but lacks a module: a wrong version, not a missing extra."""
    (tmp_path / "present_pkg_for_this_test").mkdir()
    (tmp_path / "present_pkg_for_this_test" / "__init__.py").write_text("")
    (tmp_path / "needs_absent_sub.py").write_text("import present_pkg_for_this_test.absent\n")
    (tmp_path / "needs_absent_pkg.py").write_text("import absent_pkg_for_this_test.absent\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    needs = ("present_pkg_for_this_test", "absent_pkg_for_this_test")
    with pytest.raises(ModuleNotFoundError) as outcome:
        _entry._resolve("cmd", "mcp", "needs_absent_sub", needs)
    assert outcome.value.name == "present_pkg_for_this_test.absent"
    # Positive control: the same import shape with the whole package absent
    # does name the extra.
    with pytest.raises(SystemExit) as named:
        _entry._resolve("cmd", "mcp", "needs_absent_pkg", needs)
    assert "missing module: absent_pkg_for_this_test)" in str(named.value)


def test_both_commands_are_installed_through_the_entry_module() -> None:
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "scripts"
    ]
    assert scripts["jobd-worker"] == "jobd._entry:worker"
    assert scripts["jobd-mcp"] == "jobd._entry:mcp"
    # The two that need no extra are untouched.
    assert scripts["jobd"] == "jobd.main:run"
    assert scripts["job"] == "job_cli.cli:main"


def test_the_names_each_extra_brings_are_declared_in_pyproject() -> None:
    """``_WORKER_NEEDS`` and ``_MCP_NEEDS`` are import names; each must come
    from a distribution its extra declares, or the message names the wrong fix."""
    extras = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]
    import_name = {"pyyaml": "yaml", "nvidia-ml-py": "pynvml"}

    def brings(extra: str) -> set[str]:
        dists = [d.split(">")[0].split("<")[0].split("=")[0].strip().lower() for d in extras[extra]]
        return {import_name.get(d, d) for d in dists}

    assert set(_entry._WORKER_NEEDS) <= brings("worker"), brings("worker")
    assert set(_entry._MCP_NEEDS) <= brings("mcp"), brings("mcp")
