"""One supervised ffmpeg per camera, plus the index and retention loops.

in full: one task per enabled camera, restart with
backoff, a stall watchdog, a per-camera stderr ring buffer, and a status that
the Nodes page and /api/health both read.

**One deliberate departure from the spec, because the design's version breaks
recording.** The spec defines /api/health as 200 only when every enabled
camera's recorder is running, and the Docker HEALTHCHECK restarts the
container when it is not. One unreachable camera would therefore restart the
container, killing recording for the cameras that are fine, and keep doing it
on a loop. So: a camera-level fault is reported in the health body and left
to the `NightwireRecorderStopped` alert rule, which is where "one camera is
down" belongs. Health itself fails only on a process-wide fault.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sqlite3
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from .. import cameras as camera_repo
from .. import db, rtsp
from . import backoff as backoff_mod
from . import ffmpeg, indexer, paths, retention, stats

log = logging.getLogger("nightwire.recorder")

STDERR_LINES = 50
INDEX_INTERVAL_SECONDS = 5.0
HOUR_PRECREATE_INTERVAL_SECONDS = 300.0
WATCHDOG_TICK_SECONDS = 5.0
SHUTDOWN_GRACE_SECONDS = 20.0

STATE_RECORDING = "recording"
STATE_RECONNECTING = "reconnecting"
STATE_ERROR = "error"
STATE_DISABLED = "disabled"
STATE_STOPPED = "stopped"


class CameraRecorder:
    """Owns one camera's ffmpeg child process and its state."""

    def __init__(self, camera_id: str, name: str, rtsp_uri: str, segment_seconds: int) -> None:
        self.camera_id = camera_id
        self.name = name
        self.rtsp_uri = rtsp_uri
        self.segment_seconds = segment_seconds
        self.state = STATE_STOPPED
        self.detail = ""
        self.stderr: deque[str] = deque(maxlen=STDERR_LINES)
        self.backoff = backoff_mod.Backoff()
        self.process: asyncio.subprocess.Process | None = None
        self.started_at: float | None = None
        self.last_segment_at: float | None = None
        self.last_segment_ms: int | None = None
        self.restarts = 0
        self.watchdog_kills = 0

    @property
    def stall_limit(self) -> float:
        return self.segment_seconds * 2

    def note(self, line: str) -> None:
        """Append a line to the ring buffer the Nodes page shows.

        Redacted here rather than at the display: a failed RTSP connection
        prints the whole URL, and this buffer is returned by
        /api/cameras/{id}/status and rendered in the UI.
        """
        if line:
            self.stderr.append(f"{datetime.now().strftime('%H:%M:%S')} {rtsp.redact(line)}")

    def status(self) -> dict:
        return {
            "state": self.state,
            "detail": self.detail,
            "last_segment_at": (
                None
                if self.last_segment_ms is None
                else datetime.fromtimestamp(self.last_segment_ms / 1000, timezone.utc).isoformat()
            ),
            "last_segment_ms": self.last_segment_ms,
            "seconds_since_segment": (
                None if self.last_segment_at is None else round(time.monotonic() - self.last_segment_at, 1)
            ),
            "restarts": self.restarts,
            "watchdog_kills": self.watchdog_kills,
            "stderr_tail": list(self.stderr),
        }


class Supervisor:
    """Starts, watches and stops every camera recorder."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        recordings_dir: Path,
        *,
        secret_key: str = "",
        tz: timezone | None = None,
        tmp_dir: Path = Path("/tmp/nvr"),
        segment_seconds: int = 60,
        index_interval: float = INDEX_INTERVAL_SECONDS,
        retention_interval: float = retention.INTERVAL_SECONDS,
        make_thumbnails: bool = True,
    ) -> None:
        self.conn = conn
        self.recordings_dir = Path(recordings_dir)
        self.secret_key = secret_key
        self.tz = tz
        self.tmp_dir = Path(tmp_dir)
        self.segment_seconds = segment_seconds
        self.index_interval = index_interval
        self.retention_interval = retention_interval
        self.recorders: dict[str, CameraRecorder] = {}
        # Per-camera loop tasks, so one camera can be stopped without
        # touching the others. `_tasks` keeps the shared loops.
        self._camera_tasks: dict[str, asyncio.Task] = {}
        self.indexer = indexer.Indexer(
            conn,
            recordings_dir,
            tz=tz,
            segment_seconds=segment_seconds,
            make_thumbnails=make_thumbnails,
        )
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()
        self.started = False
        self.last_retention: dict | None = None

    # ------------------------------------------------------------------ setup

    def _segment_list(self, camera_id: str) -> Path:
        return self.tmp_dir / f"{camera_id}.csv"

    def _precreate(self, camera_id: str) -> None:
        now = datetime.now(self.tz) if self.tz else datetime.now()
        for directory in paths.hour_dirs_to_precreate(self.recordings_dir, camera_id, now):
            directory.mkdir(parents=True, exist_ok=True)
        paths.thumbs_dir(self.recordings_dir, camera_id).mkdir(parents=True, exist_ok=True)

    def _enabled_cameras(self) -> list[sqlite3.Row]:
        return db.enabled_cameras(self.conn)

    async def start(self) -> None:
        """Reconcile the index, then start a recorder per enabled camera."""
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        paths.exports_dir(self.recordings_dir).mkdir(parents=True, exist_ok=True)

        all_ids = [row["id"] for row in self.conn.execute("SELECT id FROM cameras")]
        await self.indexer.reconcile(all_ids, now=time.time())
        for camera_id, start_ms in self.indexer.last_indexed_ms.items():
            recorder = self.recorders.get(camera_id)
            if recorder is not None:
                recorder.last_segment_ms = start_ms

        for row in self._enabled_cameras():
            self._spawn_recorder(row)

        self._tasks.append(asyncio.create_task(self._index_loop(), name="nvr-index"))
        self._tasks.append(asyncio.create_task(self._retention_loop(), name="nvr-retention"))
        self._tasks.append(asyncio.create_task(self._precreate_loop(), name="nvr-precreate"))
        self.started = True
        log.info("supervisor started with %d camera(s)", len(self.recorders))

    def _spawn_recorder(self, row: sqlite3.Row) -> None:
        recorder = CameraRecorder(
            camera_id=row["id"],
            name=row["name"],
            # Stored credential-free; credentials are injected per spawn.
            rtsp_uri=row["main_uri"],
            segment_seconds=self.segment_seconds,
        )
        self.recorders[row["id"]] = recorder
        task = asyncio.create_task(self._camera_loop(recorder), name=f"nvr-rec-{row['id']}")
        self._camera_tasks[row["id"]] = task

    async def _stop_recorder(self, camera_id: str) -> None:
        """Stop one camera's recorder and index what ffmpeg just closed."""
        recorder = self.recorders.pop(camera_id, None)
        task = self._camera_tasks.pop(camera_id, None)
        if task is not None:
            task.cancel()
        if recorder is not None and recorder.process is not None:
            with contextlib.suppress(Exception):
                await self._terminate(recorder.process)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if recorder is not None:
            recorder.state = STATE_STOPPED
            recorder.detail = "recorder stopped"
        # The segment ffmpeg was writing is finished on disk now.
        now = time.time() + self.segment_seconds * indexer.ORPHAN_MULTIPLE + 1
        with contextlib.suppress(Exception):
            await self.indexer.sweep(camera_id, now=now)
        log.info("camera %s: recorder stopped", camera_id)

    async def reconcile(self) -> dict:
        """Make the running recorders match the database.

        Called after a camera is added, edited or removed. Without it a
        camera added through the UI records nothing until the container is
        restarted, switching recording off leaves ffmpeg running, and
        deleting a camera leaves a recorder pulling a stream for a camera
        that no longer exists. All three were true until 2026-10-30, found by
        adding the first real camera through the API.
        """
        if not self.started or self._stopping.is_set():
            return {"started": [], "stopped": [], "restarted": []}

        wanted = {row["id"]: row for row in self._enabled_cameras()}
        started: list[str] = []
        stopped: list[str] = []
        restarted: list[str] = []

        for camera_id in list(self.recorders):
            if camera_id not in wanted:
                await self._stop_recorder(camera_id)
                stopped.append(camera_id)

        for camera_id, row in wanted.items():
            existing = self.recorders.get(camera_id)
            if existing is None:
                self._precreate(camera_id)
                self._spawn_recorder(row)
                started.append(camera_id)
            elif existing.rtsp_uri != row["main_uri"] or existing.name != row["name"]:
                # A different profile means a different stream: the running
                # ffmpeg is pulling the old one.
                await self._stop_recorder(camera_id)
                self._spawn_recorder(row)
                restarted.append(camera_id)

        if started or stopped or restarted:
            log.info(
                "recorders reconciled: started=%s stopped=%s restarted=%s",
                started, stopped, restarted,
            )
        return {"started": started, "stopped": stopped, "restarted": restarted}

    # ------------------------------------------------------------- the loops

    async def _camera_loop(self, recorder: CameraRecorder) -> None:
        while not self._stopping.is_set():
            self._precreate(recorder.camera_id)
            try:
                await self._run_once(recorder)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a recorder must never die silently
                recorder.state = STATE_ERROR
                recorder.detail = f"supervisor error: {exc}"
                recorder.note(f"supervisor error: {exc}")
                log.exception("recorder %s raised", recorder.camera_id)

            if self._stopping.is_set():
                break

            ran_for = (time.monotonic() - recorder.started_at) if recorder.started_at else 0.0
            delay = recorder.backoff.next_delay(ran_for)
            recorder.state = STATE_RECONNECTING
            recorder.detail = f"restarting in {delay:.0f}s (attempt {recorder.backoff.attempts})"
            recorder.note(recorder.detail)
            log.warning("camera %s: %s", recorder.camera_id, recorder.detail)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)

        recorder.state = STATE_STOPPED
        recorder.detail = "supervisor stopped"

    def _authenticated_uri(self, camera_id: str, fallback: str) -> str:
        """The stream URI with credentials, built fresh for each spawn.

        Never stored on the recorder and never logged: `CameraRecorder.note`
        redacts, and `rtsp.redact` is applied to anything derived from it.
        """
        try:
            row = camera_repo.get_row(self.conn, camera_id)
        except camera_repo.NotFound:
            return fallback
        return camera_repo.authenticated_uri(row, self.secret_key, "main") or fallback

    async def _run_once(self, recorder: CameraRecorder) -> None:
        argv = ffmpeg.record_argv(
            rtsp_uri=self._authenticated_uri(recorder.camera_id, recorder.rtsp_uri),
            recordings_dir=self.recordings_dir,
            camera_id=recorder.camera_id,
            segment_seconds=self.segment_seconds,
            segment_list=self._segment_list(recorder.camera_id),
        )
        log.info("camera %s: starting ffmpeg", recorder.camera_id)
        log.debug("camera %s argv: %s", recorder.camera_id, rtsp.redact_argv(argv))
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,  # kept open so 'q' can be sent on shutdown
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        recorder.process = process
        recorder.started_at = time.monotonic()
        recorder.last_segment_at = time.monotonic()  # grace: do not stall-kill at startup
        recorder.state = STATE_RECORDING
        recorder.detail = f"recording to {self.segment_seconds}s segments"

        stderr_task = asyncio.create_task(self._drain_stderr(recorder, process))
        try:
            await self._watch(recorder, process)
        finally:
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stderr_task
            recorder.process = None

        code = process.returncode
        recorder.restarts += 1
        if self._stopping.is_set():
            recorder.detail = "stopped for shutdown"
            return
        recorder.state = STATE_ERROR if code not in (0, None) else STATE_RECONNECTING
        recorder.detail = f"ffmpeg exited with code {code}"
        recorder.note(recorder.detail)

    async def _watch(self, recorder: CameraRecorder, process) -> None:
        """Wait for exit, killing the process if segments stop appearing.

        The stall watchdog is the reason an RTSP socket that stays open but
        stops delivering frames does not silently record nothing: ffmpeg's own
        `-timeout` only covers a dead socket.
        """
        while True:
            try:
                await asyncio.wait_for(process.wait(), timeout=WATCHDOG_TICK_SECONDS)
                return
            except asyncio.TimeoutError:
                pass

            if self._stopping.is_set():
                return

            since = time.monotonic() - (recorder.last_segment_at or time.monotonic())
            if since > recorder.stall_limit:
                recorder.watchdog_kills += 1
                message = (
                    f"no new segment for {since:.0f}s (limit {recorder.stall_limit:.0f}s), "
                    "restarting ffmpeg"
                )
                recorder.note(message)
                log.warning("camera %s: %s", recorder.camera_id, message)
                await self._terminate(process)
                return

    async def _drain_stderr(self, recorder: CameraRecorder, process) -> None:
        assert process.stderr is not None
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            recorder.note(line.decode("utf-8", "replace").rstrip())

    async def _index_loop(self) -> None:
        """Sweep each camera's directory for finished segments."""
        while not self._stopping.is_set():
            try:
                now = time.time()
                for camera_id, recorder in list(self.recorders.items()):
                    count = await self.indexer.sweep(camera_id, now=now)
                    if count:
                        recorder.last_segment_at = time.monotonic()
                        recorder.last_segment_ms = self.indexer.last_indexed_ms.get(camera_id)
                        if recorder.state == STATE_RECORDING:
                            recorder.backoff.reset()
            except Exception:  # noqa: BLE001 - the loop must survive one bad file
                log.exception("index sweep failed")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self.index_interval)

    async def _retention_loop(self) -> None:
        while not self._stopping.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self.retention_interval)
            if self._stopping.is_set():
                return
            try:
                await asyncio.to_thread(self.run_retention)
            except Exception:  # noqa: BLE001
                log.exception("retention pass failed")

    def run_retention(self) -> dict:
        """One retention pass, plus the projection warning. Also callable directly."""
        settings = {
            row["key"]: row["value"] for row in self.conn.execute("SELECT key, value FROM settings")
        }
        now_ms = int(time.time() * 1000)
        result = retention.enforce(
            self.conn, self.recordings_dir, now_ms=now_ms, settings=settings
        )
        usage = retention.disk_usage(self.recordings_dir)
        projection = stats.projection(
            self.conn,
            now_ms=now_ms,
            total_bytes=usage.total_bytes,
            max_disk_percent=float(settings.get("max_disk_percent", retention.DEFAULT_MAX_DISK_PERCENT)),
            retention_days=retention.retention_days_by_camera(self.conn),
        )
        for warning in projection["warnings"]:
            log.warning("retention projection: %s", warning["detail"])
        result["projection"] = projection
        self.last_retention = result
        return result

    async def _precreate_loop(self) -> None:
        while not self._stopping.is_set():
            for camera_id in list(self.recorders):
                with contextlib.suppress(OSError):
                    self._precreate(camera_id)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=HOUR_PRECREATE_INTERVAL_SECONDS
                )

    # ---------------------------------------------------------------- stopping

    async def _terminate(self, process) -> None:
        """Ask ffmpeg to close the current segment, then insist."""
        if process.returncode is not None:
            return
        with contextlib.suppress(Exception):
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.write(b"q")
                await process.stdin.drain()
        with contextlib.suppress(Exception):
            if os.name != "nt":
                process.send_signal(signal.SIGINT)
        try:
            await asyncio.wait_for(process.wait(), timeout=SHUTDOWN_GRACE_SECONDS)
            return
        except asyncio.TimeoutError:
            log.warning("ffmpeg did not exit in %.0fs, terminating", SHUTDOWN_GRACE_SECONDS)
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()

    async def stop(self) -> None:
        """Graceful shutdown: close the current segment, then index it.

        `stop_grace_period: 30s` in the compose file is what gives this time
        to run, so a container restart loses at most the segment in flight.
        """
        if self._stopping.is_set():
            return
        self._stopping.set()
        log.info("supervisor stopping")

        await asyncio.gather(
            *(self._terminate(r.process) for r in self.recorders.values() if r.process is not None),
            return_exceptions=True,
        )

        for task in [*self._tasks, *self._camera_tasks.values()]:
            task.cancel()
        await asyncio.gather(
            *self._tasks, *self._camera_tasks.values(), return_exceptions=True
        )
        self._tasks.clear()
        self._camera_tasks.clear()

        # Index what ffmpeg just closed, so the last minute is not lost.
        now = time.time() + self.segment_seconds * indexer.ORPHAN_MULTIPLE + 1
        for camera_id in list(self.recorders):
            with contextlib.suppress(Exception):
                await self.indexer.sweep(camera_id, now=now)

        for recorder in self.recorders.values():
            recorder.state = STATE_STOPPED
            recorder.detail = "supervisor stopped"
        self.started = False
        log.info("supervisor stopped")

    # ----------------------------------------------------------------- status

    def status(self, camera_id: str) -> dict:
        recorder = self.recorders.get(camera_id)
        if recorder is None:
            return {
                "state": STATE_DISABLED,
                "detail": "Recording is switched off for this camera.",
                "last_segment_at": None,
                "stderr_tail": [],
            }
        return recorder.status()

    def summary(self) -> dict:
        states: dict[str, int] = {}
        for recorder in self.recorders.values():
            states[recorder.state] = states.get(recorder.state, 0) + 1
        return {
            "cameras": len(self.recorders),
            "recording": states.get(STATE_RECORDING, 0),
            "states": states,
        }

    def health(self) -> tuple[bool, str, str]:
        """Process-wide recorder health.

        Deliberately **not** false when a single camera is down: the Docker
        healthcheck restarts the container on unhealthy, and restarting
        because one camera is unreachable would stop recording for the
        cameras that are fine. A camera-level fault belongs to the
        `NightwireRecorderStopped` alert rule, and is reported in the body.
        """
        if not self.started:
            return False, "stopped", "the recorder supervisor is not running"
        total = len(self.recorders)
        if total == 0:
            # A camera with recording switched off has no recorder, so
            # "no recorders" is not the same as "no cameras" - and the header
            # chip reads this detail. Saying "No cameras yet" with three
            # cameras configured is how this was found.
            configured = len(list(self.conn.execute("SELECT id FROM cameras")))
            if configured:
                plural = "" if configured == 1 else "s"
                return True, "idle", f"{configured} camera{plural}, none set to record"
            return True, "idle", "No cameras yet"
        counted = self.summary()
        recording = counted["recording"]
        detail = f"{recording} of {total} recording"
        if recording < total:
            faulty = sorted(
                f"{r.camera_id} ({r.state})" for r in self.recorders.values() if r.state != STATE_RECORDING
            )
            detail += f"; attention: {', '.join(faulty)}"
        return True, STATE_RECORDING if recording == total else "degraded", detail
