"""Indexing finished segments into SQLite, and reconciling with the disk.

Two things the spec review changed, and the reasons:

1. **The indexer watches the directory, not ffmpeg's CSV.** The segment muxer
   defaults to `segment_list_flags +cache`, which only writes the list file
   when ffmpeg exits, so a 24/7 recorder would append nothing and the indexer
   would see no segments at all. `+live` is set on the command anyway, but
   nothing here depends on it.

2. **A segment counts as finished when a later one exists.** ffmpeg is still
   writing the newest file, so probing it would measure a partial duration.
   The exception is a file left behind by a crash: once it is older than a
   couple of segment lengths with nothing after it, it is finished too.

`ffprobe` is reached through an injected runner, so the indexing logic is
testable without ffmpeg installed.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import timezone
from pathlib import Path
from typing import Awaitable, Callable

from . import ffmpeg, paths

log = logging.getLogger("nightwire.indexer")

# How long a trailing segment must sit untouched before it is indexed anyway.
ORPHAN_MULTIPLE = 2

ProbeRunner = Callable[[Path], Awaitable[dict]]
ThumbRunner = Callable[[Path, Path], Awaitable[bool]]


async def run_ffprobe(segment_path: Path) -> dict:
    """Measure a segment with ffprobe. The real runner."""
    process = await asyncio.create_subprocess_exec(
        *ffmpeg.probe_argv(segment_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        log.warning("ffprobe failed on %s: %s", segment_path, stderr.decode("utf-8", "replace").strip())
        return {"duration_ms": 0, "has_audio": False}
    return ffmpeg.parse_probe(stdout)


async def run_thumbnail(segment_path: Path, thumb_path: Path) -> bool:
    """Grab one keyframe. Failure is not fatal: a hover preview is a nicety."""
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    process = await asyncio.create_subprocess_exec(
        *ffmpeg.thumbnail_argv(segment_path, thumb_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        log.debug("thumbnail failed for %s: %s", segment_path, stderr.decode("utf-8", "replace").strip())
        return False
    return True


def finished_segments(
    files: list[Path], *, now: float, segment_seconds: int, mtimes: dict[Path, float] | None = None
) -> list[Path]:
    """Of these files, the ones ffmpeg has definitely closed.

    `files` must be in chronological order. Everything but the last is
    finished; the last one counts only if it has gone quiet, which is how a
    segment orphaned by a crash or a clean shutdown still gets indexed.
    """
    if not files:
        return []

    def mtime_of(path: Path) -> float:
        if mtimes is not None and path in mtimes:
            return mtimes[path]
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    finished = list(files[:-1])
    last = files[-1]
    if now - mtime_of(last) > segment_seconds * ORPHAN_MULTIPLE:
        finished.append(last)
    return finished


def is_indexed(conn: sqlite3.Connection, relative: str) -> bool:
    row = conn.execute("SELECT 1 FROM segments WHERE path = ?", (relative,)).fetchone()
    return row is not None


def insert_segment(
    conn: sqlite3.Connection,
    *,
    camera_id: str,
    relative: str,
    start_ts: int,
    duration_ms: int,
    size_bytes: int,
    has_audio: bool,
) -> int | None:
    """Insert one segment. Returns its id, or None if it was already there.

    `path` is UNIQUE, so a concurrent reconcile and live index cannot produce
    a duplicate row; the loser is ignored rather than raising.
    """
    try:
        cursor = conn.execute(
            "INSERT INTO segments (camera_id, start_ts, end_ts, duration_ms, path, size_bytes, has_audio)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                camera_id,
                start_ts,
                start_ts + duration_ms,
                duration_ms,
                relative,
                size_bytes,
                1 if has_audio else 0,
            ),
        )
    except sqlite3.IntegrityError:
        return None
    conn.commit()
    return int(cursor.lastrowid)


class Indexer:
    """Indexes a camera's finished segments, and rebuilds the index on boot."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        recordings_dir: Path,
        *,
        tz: timezone | None = None,
        segment_seconds: int = 60,
        probe: ProbeRunner | None = None,
        thumbnail: ThumbRunner | None = None,
        make_thumbnails: bool = True,
    ) -> None:
        self.conn = conn
        self.recordings_dir = Path(recordings_dir)
        self.tz = tz
        self.segment_seconds = segment_seconds
        self.probe = probe or run_ffprobe
        self.thumbnail = thumbnail or run_thumbnail
        self.make_thumbnails = make_thumbnails
        self.last_indexed_ms: dict[str, int] = {}

    async def index_file(self, camera_id: str, segment_path: Path) -> int | None:
        relative = paths.relative_path(self.recordings_dir, segment_path)
        if is_indexed(self.conn, relative):
            return None
        try:
            stat = segment_path.stat()
        except OSError:
            return None
        if stat.st_size == 0:
            # ffmpeg creates the file before writing to it; an empty one is
            # either in flight or junk from a hard kill.
            return None

        measured = await self.probe(segment_path)
        start_ts = paths.segment_start_ms(segment_path, self.tz, mtime=stat.st_mtime)
        segment_id = insert_segment(
            self.conn,
            camera_id=camera_id,
            relative=relative,
            start_ts=start_ts,
            duration_ms=measured["duration_ms"],
            size_bytes=stat.st_size,
            has_audio=measured["has_audio"],
        )
        if segment_id is None:
            return None

        self.last_indexed_ms[camera_id] = max(self.last_indexed_ms.get(camera_id, 0), start_ts)

        if self.make_thumbnails:
            await self.thumbnail(
                segment_path, paths.thumb_path(self.recordings_dir, camera_id, segment_path)
            )
        return segment_id

    async def sweep(self, camera_id: str, *, now: float) -> int:
        """Index every finished, unindexed segment for one camera."""
        files = list(paths.iter_segment_files(self.recordings_dir, camera_id))
        indexed = 0
        for path in finished_segments(files, now=now, segment_seconds=self.segment_seconds):
            if await self.index_file(camera_id, path) is not None:
                indexed += 1
        return indexed

    async def reconcile(self, camera_ids: list[str], *, now: float) -> dict:
        """Rebuild the index from the disk, and drop rows with no file.

        This is what makes a lost database recoverable: every segment on disk
        is re-indexed, and every row pointing at a file that is gone (deleted
        outside the app, or lost with a disk) is removed so the timeline does
        not advertise footage that cannot be played.
        """
        added = 0
        for camera_id in camera_ids:
            added += await self.sweep(camera_id, now=now)

        removed = 0
        for row in list(self.conn.execute("SELECT id, path FROM segments")):
            if not paths.absolute_path(self.recordings_dir, row["path"]).is_file():
                self.conn.execute("DELETE FROM segments WHERE id = ?", (row["id"],))
                removed += 1
        if removed:
            self.conn.commit()

        result = {"indexed": added, "dropped_rows": removed}
        if added or removed:
            log.info("startup reconcile: %s", result)
        return result
