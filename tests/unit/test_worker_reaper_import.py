"""S4 spec-review regression: jobd.worker.job_worker must successfully bind
the subreaper + cgroup_walk modules at module-load time.

The original S4 commit shipped an EMPTY try-block (no `from jobd import ...`
inside it), so _REAPER_OK was True but the names `_subreaper` / `_cgroup_walk`
were never bound. Any job-finalize path hit NameError at runtime; the
existing tests passed because they import jobd.subreaper / jobd.cgroup_walk
directly, bypassing the worker module.

The worker now ships inside the jobd package (jobd.worker.job_worker) and
imports its siblings directly, so the names are bound unconditionally and a
broken import fails loudly at load. This test still guards the original
intent: the names must actually resolve to the real modules, not be left
unbound by a silent fall-through.
"""

from __future__ import annotations

import jobd.worker.job_worker as worker_module


def test_worker_reaper_import_bound():
    """The module-top imports must actually bind the modules, not just flip
    _REAPER_OK to True. Regression for the S4 first-pass empty-try bug.
    """
    assert worker_module._REAPER_OK is True

    # The two names the job-finalize path references.
    assert hasattr(worker_module, "_subreaper"), "_subreaper not bound"
    assert hasattr(worker_module, "_cgroup_walk"), "_cgroup_walk not bound"

    # Sanity: the bound modules are the real ones, not None / placeholder.
    assert callable(worker_module._subreaper.set_child_subreaper)
    assert callable(worker_module._cgroup_walk.resolve_user_scope_path)
    assert callable(worker_module._cgroup_walk.kill_scope)
