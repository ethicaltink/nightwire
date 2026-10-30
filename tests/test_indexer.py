"""The indexer, with ffprobe and ffmpeg replaced by fakes.

No ffmpeg binary and no camera is needed: what is under test is finished-
segment detection, insertion, and the startup reconcile.
"""

import asyncio
from datetime import timezone
from pathlib import Path

from nightwire import cameras, db
from nightwire.recording import indexer, paths

SECRET = "s" * 40
# Close to the fixture filenames below on purpose: a filename more than a day
# from the file's mtime is deliberately overridden by the mtime (see
# paths.segment_start_ms), which would make these fixtures meaningless.
NOW = 1793112000.0  # 2026-10-27 14:40:00 UTC


def run(coro):
    return asyncio.run(coro)


def fresh(tmp_path, camera_ids=("front",)):
    conn = db.connect(tmp_path / "nightwire.db")
    db.migrate(conn)
    for camera_id in camera_ids:
        cameras.create(
            conn,
            SECRET,
            camera_id=camera_id,
            name=camera_id,
            host="192.168.1.101",
            main_uri="rtsp://x",
        )
    return conn


def write_segment(recordings, camera, name, size=1024, mtime=None):
    path = recordings / camera / "2026" / "10" / "27" / "14" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    if mtime is not None:
        import os

        os.utime(path, (mtime, mtime))
    return path


def make_indexer(conn, recordings, duration_ms=60000, has_audio=False, thumbs=True):
    calls = {"probe": [], "thumb": []}

    async def probe(path):
        calls["probe"].append(path)
        return {"duration_ms": duration_ms, "has_audio": has_audio}

    async def thumbnail(segment, thumb):
        calls["thumb"].append((segment, thumb))
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"t")
        return True

    index = indexer.Indexer(
        conn,
        recordings,
        tz=timezone.utc,
        segment_seconds=60,
        probe=probe,
        thumbnail=thumbnail,
        make_thumbnails=thumbs,
    )
    return index, calls


# ------------------------------------------------- finished-segment detection


def test_the_newest_file_is_still_being_written():
    files = [Path("20261027T143100.ts"), Path("20261027T143200.ts")]
    mtimes = {files[0]: NOW - 60, files[1]: NOW - 1}
    finished = indexer.finished_segments(files, now=NOW, segment_seconds=60, mtimes=mtimes)
    assert finished == [files[0]]


def test_a_quiet_trailing_segment_is_indexed_anyway():
    """A segment orphaned by a crash or a clean shutdown must not be lost."""
    files = [Path("20261027T143100.ts")]
    mtimes = {files[0]: NOW - 500}
    finished = indexer.finished_segments(files, now=NOW, segment_seconds=60, mtimes=mtimes)
    assert finished == files


def test_no_files_is_not_an_error():
    assert indexer.finished_segments([], now=NOW, segment_seconds=60) == []


def test_everything_but_the_last_is_finished():
    files = [Path(f"20261027T1431{i:02d}.ts") for i in range(5)]
    mtimes = {p: NOW - 1 for p in files}
    finished = indexer.finished_segments(files, now=NOW, segment_seconds=60, mtimes=mtimes)
    assert finished == files[:-1]


# ------------------------------------------------------------------ indexing


def test_sweep_indexes_closed_segments_only(tmp_path):
    conn = fresh(tmp_path)
    recordings = tmp_path / "recordings"
    write_segment(recordings, "front", "20261027T143100.ts", mtime=NOW - 120)
    write_segment(recordings, "front", "20261027T143200.ts", mtime=NOW - 1)
    index, calls = make_indexer(conn, recordings)

    assert run(index.sweep("front", now=NOW)) == 1
    rows = list(conn.execute("SELECT * FROM segments"))
    assert len(rows) == 1
    assert rows[0]["path"] == "front/2026/10/27/14/20261027T143100.ts"
    assert rows[0]["duration_ms"] == 60000
    assert rows[0]["size_bytes"] == 1024
    assert rows[0]["end_ts"] == rows[0]["start_ts"] + 60000
    assert len(calls["probe"]) == 1


def test_sweep_is_idempotent(tmp_path):
    conn = fresh(tmp_path)
    recordings = tmp_path / "recordings"
    write_segment(recordings, "front", "20261027T143100.ts", mtime=NOW - 500)
    index, _ = make_indexer(conn, recordings)
    assert run(index.sweep("front", now=NOW)) == 1
    assert run(index.sweep("front", now=NOW)) == 0
    assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 1


def test_a_thumbnail_is_written_per_segment(tmp_path):
    conn = fresh(tmp_path)
    recordings = tmp_path / "recordings"
    segment = write_segment(recordings, "front", "20261027T143100.ts", mtime=NOW - 500)
    index, calls = make_indexer(conn, recordings)
    run(index.sweep("front", now=NOW))
    thumb = paths.thumb_path(recordings, "front", segment)
    assert thumb.is_file()
    assert len(calls["thumb"]) == 1


def test_thumbnails_can_be_switched_off(tmp_path):
    conn = fresh(tmp_path)
    recordings = tmp_path / "recordings"
    write_segment(recordings, "front", "20261027T143100.ts", mtime=NOW - 500)
    index, calls = make_indexer(conn, recordings, thumbs=False)
    run(index.sweep("front", now=NOW))
    assert calls["thumb"] == []


def test_an_empty_file_is_skipped(tmp_path):
    conn = fresh(tmp_path)
    recordings = tmp_path / "recordings"
    write_segment(recordings, "front", "20261027T143100.ts", size=0, mtime=NOW - 500)
    index, calls = make_indexer(conn, recordings)
    assert run(index.sweep("front", now=NOW)) == 0
    assert calls["probe"] == []


def test_audio_is_recorded_when_present(tmp_path):
    conn = fresh(tmp_path)
    recordings = tmp_path / "recordings"
    write_segment(recordings, "front", "20261027T143100.ts", mtime=NOW - 500)
    index, _ = make_indexer(conn, recordings, has_audio=True)
    run(index.sweep("front", now=NOW))
    assert conn.execute("SELECT has_audio FROM segments").fetchone()[0] == 1


def test_an_unmeasurable_duration_is_stored_as_zero_not_guessed(tmp_path):
    conn = fresh(tmp_path)
    recordings = tmp_path / "recordings"
    write_segment(recordings, "front", "20261027T143100.ts", mtime=NOW - 500)
    index, _ = make_indexer(conn, recordings, duration_ms=0)
    run(index.sweep("front", now=NOW))
    row = conn.execute("SELECT duration_ms, end_ts, start_ts FROM segments").fetchone()
    assert row["duration_ms"] == 0
    assert row["end_ts"] == row["start_ts"]


def test_last_indexed_is_tracked_per_camera(tmp_path):
    conn = fresh(tmp_path, ("front", "yard"))
    recordings = tmp_path / "recordings"
    write_segment(recordings, "front", "20261027T143100.ts", mtime=NOW - 500)
    write_segment(recordings, "yard", "20261027T140000.ts", mtime=NOW - 500)
    index, _ = make_indexer(conn, recordings)
    run(index.sweep("front", now=NOW))
    run(index.sweep("yard", now=NOW))
    assert set(index.last_indexed_ms) == {"front", "yard"}
    assert index.last_indexed_ms["front"] > index.last_indexed_ms["yard"]


# ---------------------------------------------------------------- reconcile


def test_reconcile_rebuilds_a_lost_database(tmp_path):
    conn = fresh(tmp_path)
    recordings = tmp_path / "recordings"
    for name in ["20261027T143100.ts", "20261027T143200.ts", "20261027T143300.ts"]:
        write_segment(recordings, "front", name, mtime=NOW - 500)
    index, _ = make_indexer(conn, recordings)

    result = run(index.reconcile(["front"], now=NOW))
    assert result["indexed"] == 3
    assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 3


def test_reconcile_drops_rows_whose_file_is_gone(tmp_path):
    conn = fresh(tmp_path)
    recordings = tmp_path / "recordings"
    conn.execute(
        "INSERT INTO segments (camera_id, start_ts, end_ts, duration_ms, path, size_bytes)"
        " VALUES ('front', 1, 2, 1000, 'front/2026/10/27/14/vanished.ts', 10)"
    )
    conn.commit()
    index, _ = make_indexer(conn, recordings)

    result = run(index.reconcile(["front"], now=NOW))
    assert result["dropped_rows"] == 1
    assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 0


def test_reconcile_is_a_no_op_on_a_clean_index(tmp_path):
    conn = fresh(tmp_path)
    recordings = tmp_path / "recordings"
    write_segment(recordings, "front", "20261027T143100.ts", mtime=NOW - 500)
    index, _ = make_indexer(conn, recordings)
    run(index.reconcile(["front"], now=NOW))
    assert run(index.reconcile(["front"], now=NOW)) == {"indexed": 0, "dropped_rows": 0}


def test_reconcile_ignores_files_that_are_not_segments(tmp_path):
    conn = fresh(tmp_path)
    recordings = tmp_path / "recordings"
    stray = recordings / "front" / "2026" / "10" / "27" / "14" / "notes.txt"
    stray.parent.mkdir(parents=True)
    stray.write_text("x")
    index, _ = make_indexer(conn, recordings)
    assert run(index.reconcile(["front"], now=NOW))["indexed"] == 0
