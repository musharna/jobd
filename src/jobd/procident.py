"""Identify a process jobd did not start (docs/adoption.md).

A PID alone does not name a process: the kernel recycles it. The pair
(pid, start time) does. The start time is `/proc/<pid>/stat` field 22 — clock
ticks since boot, fixed for the life of the process. Shared by the CLI (which
reads it when the user says `job adopt --pid N`) and the worker (which compares
it before watching or signalling that PID).

Linux only: there is no `/proc/<pid>/stat` elsewhere, and saying so beats
guessing at an equivalent.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

_PROC = Path("/proc")

# Heartbeat tag a worker carries when it can watch an adopted process. The
# broker refuses `/adopt` onto a worker without it: a worker that predates
# adoption would never report the job in flight, and the user would be told
# "running" about something nobody is watching.
ADOPT_WORKER_TAG = "jobd-adopt"


class AdoptUnsupported(RuntimeError):
    """This platform cannot identify a foreign process the way adoption needs."""


@dataclass(frozen=True)
class ProcStat:
    start_ticks: int
    state: str  # one letter; "Z" = exited but not yet reaped by its parent


def _require_linux() -> None:
    if not sys.platform.startswith("linux"):
        raise AdoptUnsupported(
            f"process adoption needs Linux /proc; this platform is {sys.platform!r}"
        )


def parse_stat(text: str) -> ProcStat:
    """Parse the contents of `/proc/<pid>/stat`.

    Field 2 (comm) is parenthesised and may itself contain spaces and `)`, so
    split on the LAST `)`; what follows is field 3 onward, space-separated.
    """
    head, sep, tail = text.rpartition(")")
    fields = tail.split()
    if not sep or len(fields) < 20:
        raise ValueError(f"unparseable /proc stat line: {text[:120]!r}")
    return ProcStat(start_ticks=int(fields[19]), state=fields[0])


def read_stat(pid: int) -> ProcStat | None:
    """The process's stat, or None when no such PID exists."""
    _require_linux()
    try:
        text = (_PROC / str(pid) / "stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return parse_stat(text)


def is_same_live_process(pid: int, start_ticks: int) -> bool:
    """True iff `pid` exists, started at `start_ticks`, and has not exited."""
    st = read_stat(pid)
    return st is not None and st.start_ticks == start_ticks and st.state != "Z"


def read_owner_uid(pid: int) -> int:
    return os.stat(_PROC / str(pid)).st_uid


@dataclass(frozen=True)
class ProcIdentity:
    pid: int
    start_ticks: int
    cmd: list[str]
    cwd: str


def read_identity(pid: int) -> ProcIdentity:
    """Everything `job adopt` records about a PID. Raises with the reason when
    the process cannot be adopted; never returns a partial identity."""
    st = read_stat(pid)
    if st is None:
        raise ProcessLookupError(f"no such process: pid {pid}")
    if st.state == "Z":
        raise ProcessLookupError(f"pid {pid} has already exited (zombie)")
    raw = (_PROC / str(pid) / "cmdline").read_bytes()
    cmd = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
    if not cmd:
        raise ValueError(f"pid {pid} has no command line (a kernel thread?); refusing to adopt")
    # PermissionError here means the process belongs to another user — which the
    # worker would refuse anyway (adopt_uid_mismatch). Let it propagate.
    cwd = os.readlink(_PROC / str(pid) / "cwd")
    return ProcIdentity(pid=pid, start_ticks=st.start_ticks, cmd=cmd, cwd=cwd)
