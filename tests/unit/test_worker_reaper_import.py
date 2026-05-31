"""S4 spec-review regression: worker/job_worker.py must successfully import
the subreaper + cgroup_walk modules at module-load time.

The original S4 commit shipped an EMPTY try-block (no `from jobd import ...`
inside it), so _REAPER_OK was True but the names `_subreaper` / `_cgroup_walk`
were never bound. Any job-finalize path hit NameError at runtime; the
existing tests passed because they import jobd.subreaper / jobd.cgroup_walk
directly, bypassing the worker module.

This test loads worker/job_worker.py the same way the production worker
does (worker dir on sys.path) and asserts the names are bound and
_REAPER_OK is True on this Linux dev box.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parent.parent.parent
_WORKER_DIR = _REPO / "worker"
_SRC_DIR = _REPO / "src"


@pytest.fixture
def worker_module():
    """Import worker/job_worker.py with the same sys.path it has at runtime.

    The worker is a single-file deploy that lives outside the src/ tree.
    Its module-top sys.path.insert(0, ..parent/'src') is what makes
    `from jobd import subreaper` resolvable.
    """
    # Prepend BOTH paths so the worker's own `sys.path.insert(... 'src')`
    # has a place to land — and so we don't shadow the installed jobd
    # package with a stale copy.
    for p in (str(_WORKER_DIR), str(_SRC_DIR)):
        if p not in sys.path:
            sys.path.insert(0, p)
    # Force a fresh import so a prior test's import doesn't mask a regression.
    sys.modules.pop("job_worker", None)
    mod = importlib.import_module("job_worker")
    try:
        yield mod
    finally:
        sys.modules.pop("job_worker", None)


def test_worker_reaper_import_bound(worker_module):
    """The try-block at module top must actually import the modules, not
    just flip _REAPER_OK to True with empty bodies. Regression for the
    S4 first-pass empty-try bug.
    """
    assert worker_module._REAPER_OK is True, (
        "Expected _REAPER_OK=True on Linux dev host; if this fails check that"
        " the from-imports inside the try-block actually exist."
    )
    # The two names the job-finalize path references.
    assert hasattr(worker_module, "_subreaper"), (
        "_subreaper not bound — the try-block likely fell through silently."
    )
    assert hasattr(worker_module, "_cgroup_walk"), (
        "_cgroup_walk not bound — the try-block likely fell through silently."
    )
    # Sanity: the bound modules are the real ones, not None / placeholder.
    assert callable(worker_module._subreaper.set_child_subreaper)
    assert callable(worker_module._cgroup_walk.resolve_user_scope_path)
    assert callable(worker_module._cgroup_walk.kill_scope)
