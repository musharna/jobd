"""The worker logs with real levels, and systemd records them as such.

The worker used to emit all 41 of its diagnostics with `print(..., file=sys.stderr)`.
Under systemd (`StandardError=journal`) every line from that stream is tagged with a
single priority, so a routine "starting job 2941" and a genuine "heartbeat error" were
recorded identically — and `journalctl -p err` returned everything, which is the same as
returning nothing.

systemd parses a leading `<N>` kernel-style priority prefix, so emitting it gives real
levels with no dependency on python-systemd. It must appear ONLY when systemd is actually
consuming the stream (it sets JOURNAL_STREAM for exactly that), or an interactive
`jobd-worker` run becomes unreadable noise.
"""

from __future__ import annotations

import logging

from jobd.worker.job_worker import _JournalPriorityFormatter


def _record(level: int, msg: str) -> logging.LogRecord:
    return logging.LogRecord("jobd.worker", level, __file__, 1, msg, None, None)


def test_no_priority_prefix_when_not_under_systemd():
    """An interactive run must stay readable — no `<6>` litter."""
    f = _JournalPriorityFormatter(journal=False)
    out = f.format(_record(logging.INFO, "starting job 2941"))
    assert out == "[worker] starting job 2941", out
    assert not out.startswith("<")


def test_levels_map_to_syslog_priorities_under_systemd():
    """ERROR must be journald priority 3 — that is what makes `-p err` mean something."""
    f = _JournalPriorityFormatter(journal=True)
    assert f.format(_record(logging.ERROR, "heartbeat error")) == "<3>[worker] heartbeat error"
    assert f.format(_record(logging.WARNING, "idle timeout")) == "<4>[worker] idle timeout"
    assert f.format(_record(logging.INFO, "starting job 1")) == "<6>[worker] starting job 1"


def test_every_line_of_a_multiline_message_carries_the_prefix():
    """Otherwise the tail lines land at the default priority and get divorced from the
    error they belong to — a traceback would be split across two priorities."""
    f = _JournalPriorityFormatter(journal=True)
    out = f.format(_record(logging.ERROR, "run_job raised:\nTraceback\n  boom"))
    assert out.splitlines() == [
        "<3>[worker] run_job raised:",
        "<3>Traceback",
        "<3>  boom",
    ], out


def test_worker_module_has_no_print_calls_left():
    """Regression guard: a new print() bypasses levels and lands back in the same hole."""
    import ast
    import pathlib

    import jobd.worker.job_worker as jw

    src = pathlib.Path(jw.__file__).read_text()
    prints = [
        n.lineno
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print"
    ]
    assert not prints, (
        f"print() is back in the worker at line(s) {prints}. Under systemd it lands at "
        "an undifferentiated priority, so it cannot be filtered and it re-creates the "
        "hole this migration closed. Use log.info/warning/error."
    )
