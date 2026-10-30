"""The export worker: one job at a time, at low priority.

requires at most one export running, under `nice`,
so a precise-cut re-encode never starves the recorders. The queue is the
`exports` table itself rather than an in-memory list, so a restart mid-export
leaves a job marked `running` that is requeued at startup instead of lost.

ffmpeg is reached through an injected runner, so the queue behaviour is
testable without ffmpeg installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from ..playback.timeline import Segment
from ..recording import paths, retention
from . import ffmpeg as export_ffmpeg
from . import plan as plan_mod

log = logging.getLogger("nightwire.export")

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

POLL_SECONDS = 1.0

# (argv, output) -> (ok, stderr_tail)
Runner = Callable[[list[str], Path], Awaitable[tuple[bool, str]]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


MIN_OUTPUT_BYTES = 1024


async def verify_output(output: Path) -> str | None:
    """Check an export is actually playable. Returns a reason, or None.

    ffmpeg can exit 0 having written a valid but **empty** MP4 - a few
    hundred bytes of container with no tracks in it - for instance when the
    source codec cannot be muxed into MP4 by stream copy. A file like that
    passes an "exists and is non-empty" check and then will not play
    anywhere, so the export is probed before it is called done.
    """
    if not output.is_file():
        return "ffmpeg produced no output file"
    size = output.stat().st_size
    if size < MIN_OUTPUT_BYTES:
        return f"the clip is only {size} bytes, which cannot be real video"

    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-hide_banner",
        "-loglevel",
        "error",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(output),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _stderr = await process.communicate()
    if process.returncode != 0:
        return "the clip could not be read back by ffprobe"
    if "video" not in stdout.decode("utf-8", "replace").split():
        return (
            "the clip came out with no video track. The recorded stream's codec "
            "may not be storable in MP4 by stream copy; try the precise-cut option."
        )
    return None


async def run_ffmpeg(argv: list[str], output: Path) -> tuple[bool, str]:
    """The real runner."""
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await process.communicate()
    tail = stderr.decode("utf-8", "replace").strip()[-2000:]
    if process.returncode != 0:
        return False, tail or f"ffmpeg exited with code {process.returncode}"
    problem = await verify_output(output)
    if problem:
        return False, f"{problem} {tail}".strip()
    return True, tail


def segments_for(conn: sqlite3.Connection, camera_id: str, start_ms: int, end_ms: int):
    """Segments that might overlap a window, widened like the playlist query."""
    from ..playback.timeline import DAY_MS

    rows = conn.execute(
        "SELECT id, start_ts, end_ts, duration_ms, path, size_bytes, has_audio"
        " FROM segments WHERE camera_id = ? AND start_ts >= ? AND start_ts < ?"
        " ORDER BY start_ts",
        (camera_id, start_ms - DAY_MS, end_ms),
    )
    return [
        Segment(
            id=row["id"],
            start_ts=row["start_ts"],
            end_ts=row["end_ts"],
            duration_ms=row["duration_ms"],
            path=row["path"],
            size_bytes=row["size_bytes"],
            has_audio=bool(row["has_audio"]),
        )
        for row in rows
    ]


def row_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["precise"] = bool(row["precise"])
    data["has_gaps"] = bool(row["has_gaps"])
    if data.get("status") == DONE and data.get("path"):
        data["download_url"] = f"/api/exports/{row['id']}/download"
    else:
        data["download_url"] = None
    return data


def list_exports(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM exports ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),)
    )
    return [row_to_dict(row) for row in rows]


def get_export(conn: sqlite3.Connection, export_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM exports WHERE id = ?", (export_id,)).fetchone()
    return None if row is None else row_to_dict(row)


class ExportWorker:
    """Accepts jobs and runs them one at a time."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        recordings_dir: Path,
        *,
        tmp_dir: Path = Path("/tmp/nvr"),
        tz=None,
        runner: Runner | None = None,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self.conn = conn
        self.recordings_dir = Path(recordings_dir)
        self.tmp_dir = Path(tmp_dir)
        self.tz = tz
        self.runner = runner or run_ffmpeg
        self.poll_seconds = poll_seconds
        self.current: str | None = None
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._wake = asyncio.Event()
        self.started = False

    # ------------------------------------------------------------------ setup

    async def start(self) -> None:
        paths.exports_dir(self.recordings_dir).mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        requeued = self.requeue_interrupted()
        if requeued:
            log.info("requeued %d export(s) interrupted by a restart", requeued)
        self._task = asyncio.create_task(self._loop(), name="nvr-export")
        self.started = True

    def requeue_interrupted(self) -> int:
        """A job marked `running` with no process behind it was interrupted."""
        cursor = self.conn.execute(
            "UPDATE exports SET status = ?, error = ? WHERE status = ?",
            (QUEUED, "requeued after a restart", RUNNING),
        )
        self.conn.commit()
        return cursor.rowcount or 0

    async def stop(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self.started = False

    # ------------------------------------------------------------------ queue

    def settings(self) -> dict:
        return {
            row["key"]: row["value"] for row in self.conn.execute("SELECT key, value FROM settings")
        }

    def capacity_check(self) -> None:
        """Refuse a new job when the export store is full.

        Exports are exempt from retention, so without this the disk guard
        would start deleting *footage* to make room for saved clips. The cap
        blocks new jobs; it never deletes a clip.
        """
        cap = float(self.settings().get("max_exports_gb", retention.DEFAULT_MAX_EXPORTS_GB))
        exceeded, used, limit = retention.export_cap_exceeded(self.recordings_dir, cap)
        if exceeded:
            raise plan_mod.ExportError(
                f"The export store is full: {used / 1000**3:.1f} GB of {limit / 1000**3:.0f} GB. "
                "Delete some clips from Vault before making another."
            )

    def submit(self, camera_id: str, start_ms: int, end_ms: int, *, precise: bool = False) -> dict:
        """Validate and enqueue. Raises ExportError if it cannot be made."""
        self.capacity_check()
        built = plan_mod.build(
            camera_id,
            segments_for(self.conn, camera_id, start_ms, end_ms),
            start_ms,
            end_ms,
            precise=precise,
            tz=self.tz,
        )
        export_id = uuid.uuid4().hex[:16]
        self.conn.execute(
            "INSERT INTO exports (id, camera_id, start_ts, end_ts, precise, status, created_at,"
            " name, duration_ms, segment_count, has_gaps)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                export_id,
                camera_id,
                built.start_ts,
                built.end_ts,
                1 if precise else 0,
                QUEUED,
                _now(),
                built.name,
                built.duration_ms,
                len(built.segments),
                1 if built.has_gaps else 0,
            ),
        )
        self.conn.commit()
        self._wake.set()
        log.info("export %s queued: %s", export_id, built.name)
        return get_export(self.conn, export_id)

    def delete(self, export_id: str) -> dict:
        """Remove a clip and its row. The only thing that deletes an export."""
        row = self.conn.execute("SELECT * FROM exports WHERE id = ?", (export_id,)).fetchone()
        if row is None:
            raise LookupError(export_id)
        if row["status"] == RUNNING:
            raise plan_mod.ExportError("That export is still being made. Wait for it to finish.")

        removed = False
        if row["path"]:
            absolute = paths.absolute_path(self.recordings_dir, row["path"])
            root = paths.exports_dir(self.recordings_dir).resolve()
            # Only ever delete inside _exports, whatever the row says.
            if root in absolute.resolve().parents and absolute.is_file():
                absolute.unlink()
                removed = True
        self.conn.execute("DELETE FROM exports WHERE id = ?", (export_id,))
        self.conn.commit()
        return {"deleted": export_id, "file_removed": removed}

    # ------------------------------------------------------------------- loop

    def _next_queued(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM exports WHERE status = ? ORDER BY created_at, id LIMIT 1", (QUEUED,)
        ).fetchone()

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            row = self._next_queued()
            if row is None:
                self._wake.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds * 10)
                continue
            try:
                await self.run_one(row["id"])
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the worker must survive a bad job
                log.exception("export %s crashed", row["id"])
                self._fail(row["id"], "the export worker crashed; see the server log")

    def _fail(self, export_id: str, message: str) -> None:
        self.conn.execute(
            "UPDATE exports SET status = ?, error = ?, finished_at = ? WHERE id = ?",
            (FAILED, message, _now(), export_id),
        )
        self.conn.commit()
        self.current = None

    async def run_one(self, export_id: str) -> dict | None:
        """Run one job to completion. Also callable directly, for tests."""
        row = self.conn.execute("SELECT * FROM exports WHERE id = ?", (export_id,)).fetchone()
        if row is None:
            return None

        self.conn.execute(
            "UPDATE exports SET status = ?, error = NULL WHERE id = ?", (RUNNING, export_id)
        )
        self.conn.commit()
        self.current = export_id

        try:
            built = plan_mod.build(
                row["camera_id"],
                segments_for(self.conn, row["camera_id"], row["start_ts"], row["end_ts"]),
                row["start_ts"],
                row["end_ts"],
                precise=bool(row["precise"]),
                tz=self.tz,
            )
        except plan_mod.ExportError as exc:
            # The footage can be gone by the time the job runs: retention may
            # have deleted it while the job sat in the queue.
            self._fail(export_id, str(exc))
            return get_export(self.conn, export_id)

        name = row["name"] or built.name
        exports_dir = paths.exports_dir(self.recordings_dir)
        # Created here, not only in start(): the volume can be remounted, or
        # the folder removed by hand, and ffmpeg will not create it.
        exports_dir.mkdir(parents=True, exist_ok=True)
        output = exports_dir / f"{export_id}_{name}"
        list_path = self.tmp_dir / f"export-{export_id}.txt"
        list_path.parent.mkdir(parents=True, exist_ok=True)
        list_path.write_text(plan_mod.concat_list(built, self.recordings_dir))

        argv = export_ffmpeg.niced(
            export_ffmpeg.export_argv(
                concat_list=list_path,
                output=output,
                seek_offset_ms=built.seek_offset_ms,
                duration_ms=built.duration_ms,
                precise=built.precise,
            )
        )

        started = time.monotonic()
        try:
            ok, detail = await self.runner(argv, output)
        finally:
            with contextlib.suppress(OSError):
                list_path.unlink()

        if not ok:
            with contextlib.suppress(OSError):
                output.unlink(missing_ok=True)
            self._fail(export_id, detail or "ffmpeg failed")
            log.warning("export %s failed: %s", export_id, detail)
            return get_export(self.conn, export_id)

        size = output.stat().st_size
        self.conn.execute(
            "UPDATE exports SET status = ?, path = ?, size_bytes = ?, finished_at = ?,"
            " error = NULL WHERE id = ?",
            (
                DONE,
                paths.relative_path(self.recordings_dir, output),
                size,
                _now(),
                export_id,
            ),
        )
        self.conn.commit()
        self.current = None
        log.info(
            "export %s done in %.1fs: %s (%.1f MB)",
            export_id,
            time.monotonic() - started,
            name,
            size / 1_000_000,
        )
        return get_export(self.conn, export_id)

    # ----------------------------------------------------------------- status

    def status(self) -> dict:
        counts: dict[str, int] = {}
        for row in self.conn.execute("SELECT status, COUNT(*) AS n FROM exports GROUP BY status"):
            counts[row["status"]] = row["n"]
        used = retention.export_store_bytes(self.recordings_dir)
        cap_gb = float(self.settings().get("max_exports_gb", retention.DEFAULT_MAX_EXPORTS_GB))
        return {
            "running": self.current,
            "queued": counts.get(QUEUED, 0),
            "done": counts.get(DONE, 0),
            "failed": counts.get(FAILED, 0),
            "store_bytes": used,
            "store_cap_bytes": int(cap_gb * 1000**3),
            "store_percent": round(used / (cap_gb * 1000**3) * 100, 1) if cap_gb > 0 else 0.0,
        }
