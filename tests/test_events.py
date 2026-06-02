"""Unit tests for events.jsonl rotation + bounded reverse-read (jobd.events)."""

import json

from jobd import events


def _write_lines(path, rows):
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _row(i, **kw):
    # ts is monotonic in i so reverse order == newest-last by index.
    base = {
        "ts": f"2026-06-01T00:{i:02d}:00+00:00",
        "source": "broker",
        "event": "job_submitted",
        "job_id": i,
        "project": "p",
        "payload": {},
    }
    base.update(kw)
    return base


# ---- append + rotation ----


def test_append_event_creates_and_appends(tmp_path):
    events.append_event(tmp_path, _row(0))
    events.append_event(tmp_path, _row(1))
    lines = (tmp_path / events.EVENTS_FILENAME).read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["job_id"] == 0


def test_max_bytes_env_override(monkeypatch):
    monkeypatch.setenv("JOBD_EVENTS_MAX_BYTES", "1234")
    assert events.max_bytes() == 1234
    monkeypatch.setenv("JOBD_EVENTS_MAX_BYTES", "nonsense")
    assert events.max_bytes() == events.DEFAULT_MAX_BYTES  # fail-soft
    monkeypatch.setenv("JOBD_EVENTS_MAX_BYTES", "-5")
    assert events.max_bytes() == events.DEFAULT_MAX_BYTES  # non-positive ignored


def _surviving_job_ids(tmp_path):
    """Job ids currently on disk (backup older, live newer), oldest→newest."""
    ids = []
    for fname in (events.ROTATED_FILENAME, events.EVENTS_FILENAME):
        p = tmp_path / fname
        if p.exists():
            ids += [json.loads(line)["job_id"] for line in p.read_text().splitlines()]
    return ids


def test_rotation_moves_oversized_file_to_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBD_EVENTS_MAX_BYTES", "200")  # tiny threshold → frequent rotation
    for i in range(20):
        events.append_event(tmp_path, _row(i))
    assert (tmp_path / events.ROTATED_FILENAME).exists(), "oversized file should rotate to .1"
    survivors = _surviving_job_ids(tmp_path)
    # Retention is bounded (~2x threshold), so the oldest rows are pruned — but
    # the survivors are always a contiguous newest-suffix ending at the last row.
    assert survivors[-1] == 19, "newest row must survive"
    assert survivors == list(range(survivors[0], 20)), (
        "survivors must be a contiguous newest-suffix"
    )
    assert len(survivors) < 20, "tiny threshold must have pruned the oldest rows"


def test_only_one_backup_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBD_EVENTS_MAX_BYTES", "200")
    for i in range(60):  # several rotations
        events.append_event(tmp_path, _row(i))
    # Exactly one backup file — older history is pruned (retention ~2x threshold).
    assert (tmp_path / events.ROTATED_FILENAME).exists()
    assert not (tmp_path / "events.jsonl.2").exists()


# ---- reverse line iterator ----


def test_iter_lines_reverse_basic(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("A\nB\nC\n")
    assert list(events._iter_lines_reverse(p)) == ["C", "B", "A"]


def test_iter_lines_reverse_no_trailing_newline(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("A\nB")
    assert list(events._iter_lines_reverse(p)) == ["B", "A"]


def test_iter_lines_reverse_spans_chunk_boundary(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("AAA\nBBB\nCCC\n")
    # chunk_size smaller than a line forces boundary stitching.
    assert list(events._iter_lines_reverse(p, chunk_size=4)) == ["CCC", "BBB", "AAA"]


def test_iter_lines_reverse_empty_file(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("")
    assert list(events._iter_lines_reverse(p)) == []


# ---- read_events ----


def _all(_row_):
    return True


def test_read_events_newest_last(tmp_path):
    _write_lines(tmp_path / events.EVENTS_FILENAME, [_row(i) for i in range(5)])
    out = events.read_events(tmp_path, match=_all, cutoff=None, limit=1000)
    assert [r["job_id"] for r in out] == [0, 1, 2, 3, 4]  # newest-last


def test_read_events_limit_keeps_newest(tmp_path):
    _write_lines(tmp_path / events.EVENTS_FILENAME, [_row(i) for i in range(10)])
    out = events.read_events(tmp_path, match=_all, cutoff=None, limit=3)
    assert [r["job_id"] for r in out] == [7, 8, 9]  # newest 3, newest-last


def test_read_events_match_filters(tmp_path):
    rows = [_row(i, project=("a" if i % 2 == 0 else "b")) for i in range(6)]
    _write_lines(tmp_path / events.EVENTS_FILENAME, rows)
    out = events.read_events(
        tmp_path, match=lambda r: r.get("project") == "a", cutoff=None, limit=1000
    )
    assert [r["job_id"] for r in out] == [0, 2, 4]


def test_read_events_skips_legacy_and_malformed(tmp_path):
    p = tmp_path / events.EVENTS_FILENAME
    with p.open("w") as f:
        f.write(json.dumps(_row(0)) + "\n")
        f.write("{not json\n")  # malformed
        f.write(json.dumps({"ts": "x", "event": "legacy"}) + "\n")  # no source
        f.write(json.dumps(_row(1)) + "\n")
    out = events.read_events(tmp_path, match=lambda r: "source" in r, cutoff=None, limit=1000)
    assert [r["job_id"] for r in out] == [0, 1]


def test_read_events_cutoff_early_stops(tmp_path):
    from datetime import UTC, datetime

    _write_lines(tmp_path / events.EVENTS_FILENAME, [_row(i) for i in range(10)])
    # cutoff at minute 05 → only rows 5..9 (ts >= cutoff).
    cutoff = datetime(2026, 6, 1, 0, 5, 0, tzinfo=UTC)
    out = events.read_events(tmp_path, match=_all, cutoff=cutoff, limit=1000)
    assert [r["job_id"] for r in out] == [5, 6, 7, 8, 9]


def test_read_events_merges_rotated_backup_newest_last(tmp_path):
    # Backup holds the older half, live holds the newer half (real rotation order).
    _write_lines(tmp_path / events.ROTATED_FILENAME, [_row(i) for i in range(5)])
    _write_lines(tmp_path / events.EVENTS_FILENAME, [_row(i) for i in range(5, 10)])
    out = events.read_events(tmp_path, match=_all, cutoff=None, limit=1000)
    assert [r["job_id"] for r in out] == list(range(10))  # backup then live, newest-last


def test_read_events_limit_does_not_cross_into_backup_when_live_suffices(tmp_path):
    _write_lines(tmp_path / events.ROTATED_FILENAME, [_row(i) for i in range(5)])
    _write_lines(tmp_path / events.EVENTS_FILENAME, [_row(i) for i in range(5, 10)])
    out = events.read_events(tmp_path, match=_all, cutoff=None, limit=2)
    assert [r["job_id"] for r in out] == [8, 9]  # newest 2, only from live


def test_read_events_missing_files_returns_empty(tmp_path):
    assert events.read_events(tmp_path, match=_all, cutoff=None, limit=10) == []
