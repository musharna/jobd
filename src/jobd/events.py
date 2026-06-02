"""events.jsonl rotation + bounded reverse-read.

The broker appends one JSON line per event to ``logs_dir/events.jsonl`` (the
single write choke-point is ``app._emit_event``). Two robustness concerns at
scale, both handled here:

1. **Unbounded growth.** When the live file crosses ``max_bytes`` it is rotated
   to ``events.jsonl.1`` (one backup, overwriting any prior one), so a fresh
   file starts. Retention is therefore ~2x ``max_bytes``: the current file
   (<= max_bytes at rotation time) plus one backup. Override the threshold with
   ``JOBD_EVENTS_MAX_BYTES``.

2. **Full-file scan per query.** ``read_events`` reads newest→oldest (live file
   then the rotated backup) and, when a ``since`` cutoff is given, stops at the
   first row older than the cutoff. This is sound because the broker is a single
   process that stamps ``ts`` server-side at append time, so file order is ts
   order — once a backward scan crosses the cutoff, every earlier row is older.

Writes are serialized by a module lock so a concurrent rotate+append (FastAPI
runs sync endpoints in a threadpool) can't drop the file mid-rotation.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

EVENTS_FILENAME = "events.jsonl"
ROTATED_FILENAME = "events.jsonl.1"
DEFAULT_MAX_BYTES = 50 * 1024 * 1024  # 50 MB

_write_lock = threading.Lock()


def max_bytes() -> int:
    """Rotation threshold in bytes. ``JOBD_EVENTS_MAX_BYTES`` overrides the
    50 MB default; a non-positive or unparseable value falls back to the
    default (fail-soft — observability config must not wedge the broker)."""
    raw = os.environ.get("JOBD_EVENTS_MAX_BYTES", "").strip()
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return DEFAULT_MAX_BYTES


def _maybe_rotate(logs_dir: Path) -> None:
    """Move the live events file to the ``.1`` backup if it exceeds max_bytes.

    Caller must hold ``_write_lock``. ``os.replace`` is atomic and overwrites
    any existing backup. No-op if the live file is absent or under threshold.
    """
    live = logs_dir / EVENTS_FILENAME
    try:
        size = live.stat().st_size
    except FileNotFoundError:
        return
    if size < max_bytes():
        return
    os.replace(live, logs_dir / ROTATED_FILENAME)


def append_event(logs_dir: Path, row: dict) -> None:
    """Append one event row as a JSON line, rotating first if oversized.

    Serialized by ``_write_lock`` so a concurrent rotate+append can't race
    (check-size + os.replace + open-append must be atomic w.r.t. other writers).
    """
    line = json.dumps(row, default=str) + "\n"
    with _write_lock:
        _maybe_rotate(logs_dir)
        with (logs_dir / EVENTS_FILENAME).open("a") as f:
            f.write(line)


def _parse_ts(ts_raw: object) -> datetime | None:
    """Parse an event's ``ts`` to an aware UTC datetime, or None if absent/bad."""
    if not isinstance(ts_raw, str):
        return None
    try:
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


def _iter_lines_reverse(path: Path, chunk_size: int = 65536) -> Iterator[str]:
    """Yield text lines from ``path`` newest(last)→oldest(first) without loading
    the whole file. Reads fixed-size chunks from the end and stitches lines that
    span chunk boundaries; handles files with or without a trailing newline.
    """
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        buf = b""
        while pos > 0:
            read = min(chunk_size, pos)
            pos -= read
            f.seek(pos)
            buf = f.read(read) + buf
            parts = buf.split(b"\n")
            # parts[0] may be an incomplete line continued in an earlier chunk —
            # hold it back; the rest are complete lines, newest-last.
            buf = parts[0]
            for ln in reversed(parts[1:]):
                if ln:
                    yield ln.decode("utf-8", "replace")
        if buf:
            yield buf.decode("utf-8", "replace")


def read_events(
    logs_dir: Path,
    *,
    match: Callable[[dict], bool],
    cutoff: datetime | None,
    limit: int,
) -> list[dict]:
    """Return up to ``limit`` matching event rows, newest-LAST.

    Reads the live file then the rotated backup, newest→oldest, applying
    ``match`` (project/event/job_id/source/legacy filters live in the caller's
    predicate). When ``cutoff`` is set, stops at the first row strictly older
    than it — safe because events are append-ordered by the broker's
    server-side ts. Rows with unparseable/missing ts under a cutoff are skipped
    and never used as the stop signal. Malformed JSON lines are skipped.

    Equivalent to the prior full-scan ``out[-limit:]`` for a single file, plus
    it merges the rotated backup so history that crossed a rotation isn't lost.
    """
    collected: list[dict] = []
    for fname in (EVENTS_FILENAME, ROTATED_FILENAME):
        path = logs_dir / fname
        if not path.exists():
            continue
        stop = False
        for raw in _iter_lines_reverse(path):
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if cutoff is not None:
                ts = _parse_ts(row.get("ts"))
                if ts is None:
                    continue
                if ts < cutoff:
                    stop = True
                    break
            if match(row):
                collected.append(row)
                if len(collected) >= limit:
                    stop = True
                    break
        if stop:
            break
    collected.reverse()  # newest-last, matching the legacy endpoint's order
    return collected
