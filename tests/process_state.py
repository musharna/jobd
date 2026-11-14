"""Process-wide state a test can change and ``monkeypatch`` cannot put back.

The child-subreaper bit is the one that bit: ``jobd.subreaper`` sets it with a
real ``prctl`` on whatever process calls it, and in a test that process is
pytest. From then on every orphaned grandchild of any later test is reparented
to pytest and stays a zombie, and the worker's end-of-job sweep, which signals
every untracked child and then waits five seconds, pays that wait in each
``run_job`` test. One pass hid it because the tests that set the bit run last.
A second pass in the same process (mutmut makes two) started with the bit set
and nine watchdog tests ran five seconds over their limit (2026-11-14).
"""

from __future__ import annotations

import contextlib
import ctypes
import sys
from collections.abc import Iterator

PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL("libc.so.6", use_errno=True)


def child_subreaper_bit() -> int | None:
    """This process's child-subreaper bit (0 or 1); None where there is no prctl."""
    if sys.platform != "linux":
        return None
    out = ctypes.c_int(-1)
    if _libc().prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(out), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_GET_CHILD_SUBREAPER) failed")
    return out.value


def set_child_subreaper_bit(value: int) -> None:
    if _libc().prctl(PR_SET_CHILD_SUBREAPER, ctypes.c_ulong(value), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), f"prctl(PR_SET_CHILD_SUBREAPER, {value}) failed")


@contextlib.contextmanager
def child_subreaper_bit_restored() -> Iterator[None]:
    """Leave the child-subreaper bit as it was found, whatever the body did to it."""
    before = child_subreaper_bit()
    try:
        yield
    finally:
        if before is not None and child_subreaper_bit() != before:
            set_child_subreaper_bit(before)
