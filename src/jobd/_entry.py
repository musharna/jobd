"""Console-script entry points for the commands that need an optional extra.

``pip install jobd`` installs every console script, but ``jobd-worker`` and
``jobd-mcp`` import packages that only the ``worker`` and ``mcp`` extras bring.
Pointed straight at their modules, both died on a base install with a bare
``ModuleNotFoundError`` traceback. This module imports nothing optional, so the
script always loads; the real ``main`` is imported when the command runs, and a
missing package from the extra becomes one line naming the extra.

Only a package the extra itself brings, missing outright, gets that message.
Any other missing module keeps its traceback: a module of jobd's own is a bug
or a broken install, and a submodule missing from a package that is present
is a wrong version, which "not installed" would misdescribe.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

# Import names each extra brings that a base install lacks (pyproject.toml,
# [project.optional-dependencies]); tests/test_entry_points.py checks them
# against that table.
_WORKER_NEEDS = ("psutil", "pynvml")
_MCP_NEEDS = ("mcp", "jsonschema")


def _resolve(command: str, extra: str, module: str, needs: tuple[str, ...]) -> Callable[[], Any]:
    """Return ``module.main``, or exit naming ``extra`` when it is not installed."""
    try:
        loaded = importlib.import_module(module)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if missing not in needs:
            raise
        raise SystemExit(
            f"{command} needs the '{extra}' extra, which is not installed "
            f"(missing module: {missing}).\n"
            f"Install it with: pip install 'jobd[{extra}]'"
        ) from None
    main: Callable[[], Any] = loaded.main
    return main


def worker() -> Any:
    """``jobd-worker``: run the worker daemon (needs the ``worker`` extra)."""
    return _resolve("jobd-worker", "worker", "jobd.worker.job_worker", _WORKER_NEEDS)()


def mcp() -> Any:
    """``jobd-mcp``: run the MCP server on stdio (needs the ``mcp`` extra)."""
    return _resolve("jobd-mcp", "mcp", "jobd.mcp.server", _MCP_NEEDS)()
