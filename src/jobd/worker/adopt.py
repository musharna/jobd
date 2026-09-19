"""Watch a process this worker did not start (docs/adoption.md).

Nothing here launches anything. `watch` blocks until the adopted process exits
(or the worker is stopping) and says how the job should end. The exit code of a
non-child is not observable, so a natural exit is reported as exactly that —
`orphaned` / `adopted_exit_unobserved` — never as success.

Two ways to follow a foreign PID:

- pidfd (`os.pidfd_open`, Linux >= 5.3): the fd names ONE process for as long as
  it is open. It is opened first and the start time compared after, so the fd is
  known to refer to the process the user named; signals go through the fd and
  cannot reach a recycled PID.
- polling `/proc/<pid>/stat` where pidfd is unavailable: the start time is
  compared on every look and again immediately before each signal. That leaves a
  microseconds-wide check-then-kill window pidfd does not have; it is the best
  available without it.
"""

from __future__ import annotations

import errno
import logging
import os
import select
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from jobd import procident

log = logging.getLogger("jobd.worker")

EXIT_UNOBSERVED = "adopted_exit_unobserved"
REFUSE_GONE = "adopt_pid_gone"
REFUSE_MISMATCH = "adopt_pid_mismatch"
REFUSE_UID = "adopt_uid_mismatch"


class AdoptRefused(Exception):
    """The PID is not the process the user asked to adopt, or cannot be managed."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason


@dataclass(frozen=True)
class AdoptOutcome:
    final_state: str
    termination_reason: str | None


def _open_pidfd(pid: int) -> int | None:
    """A pidfd for `pid`, or None when this kernel/Python has no pidfd."""
    opener = getattr(os, "pidfd_open", None)
    if opener is None:
        return None
    try:
        return int(opener(pid))
    except ProcessLookupError:
        raise AdoptRefused(REFUSE_GONE, f"no process with pid {pid} on this host") from None
    except OSError as e:
        if e.errno == errno.ENOSYS:
            return None
        raise


def _verify(pid: int, start_ticks: int) -> None:
    st = procident.read_stat(pid)
    if st is None or st.state == "Z":
        raise AdoptRefused(REFUSE_GONE, f"no live process with pid {pid} on this host")
    if st.start_ticks != start_ticks:
        raise AdoptRefused(
            REFUSE_MISMATCH,
            f"pid {pid} started at tick {st.start_ticks}, not {start_ticks}: "
            "a different process (PID reuse, or a PID from another host)",
        )
    owner = procident.read_owner_uid(pid)
    if owner != os.getuid():
        raise AdoptRefused(
            REFUSE_UID,
            f"pid {pid} belongs to uid {owner}; this worker runs as uid {os.getuid()} "
            "and could not signal it",
        )


def watch(
    pid: int,
    start_ticks: int,
    *,
    poll_signal: Callable[[], str | None],
    stop: threading.Event,
    grace_s: float,
    interval_s: float = 2.0,
    use_pidfd: bool = True,
) -> AdoptOutcome | None:
    """Follow `pid` until it exits. Returns the terminal outcome, or None when
    `stop` was set first (worker shutting down — the process is left alone and
    nothing is reported). Raises AdoptRefused before watching if `pid` is not the
    process identified by `start_ticks`.

    `poll_signal` returns the broker's pending signal for the job; "cancel"
    sends SIGTERM, then SIGKILL once `grace_s` has passed.
    """
    fd = _open_pidfd(pid) if use_pidfd else None
    try:
        _verify(pid, start_ticks)

        def exited(wait_s: float) -> bool:
            if fd is not None:
                return bool(select.select([fd], [], [], wait_s)[0])
            if not procident.is_same_live_process(pid, start_ticks):
                return True
            stop.wait(wait_s)
            return False

        def send(sig: signal.Signals) -> None:
            try:
                if fd is not None:
                    signal.pidfd_send_signal(fd, sig)
                elif procident.is_same_live_process(pid, start_ticks):
                    os.kill(pid, sig)
            except ProcessLookupError:
                pass  # exited between the liveness look and the signal; the loop sees it

        kill_at: float | None = None
        killed = False
        while True:
            if exited(interval_s):
                if kill_at is not None:
                    return AdoptOutcome("cancelled", None)
                return AdoptOutcome("orphaned", EXIT_UNOBSERVED)
            if stop.is_set():
                return None
            if kill_at is None:
                if poll_signal() == "cancel":
                    log.info("adopted pid %s: cancel requested, sending SIGTERM", pid)
                    send(signal.SIGTERM)
                    kill_at = time.monotonic() + grace_s
            elif not killed and time.monotonic() >= kill_at:
                log.warning("adopted pid %s: cancel grace %ss expired, SIGKILL", pid, grace_s)
                send(signal.SIGKILL)
                killed = True
    finally:
        if fd is not None:
            os.close(fd)
