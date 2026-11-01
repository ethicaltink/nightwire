"""Supervising the go2rtc child process.

Same shape as the recorder's supervisor and the same backoff, but a much
smaller job: one process, restarted on exit, with streams re-registered once
it answers. Recording does not depend on this process, so a go2rtc crash
costs live video and nothing else.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sqlite3
import time
from collections import deque
from pathlib import Path

from .. import db, rtsp
from ..recording import backoff as backoff_mod
from . import client, config

log = logging.getLogger("nightwire.go2rtc")

BINARY = "go2rtc"
STDERR_LINES = 50
READY_TIMEOUT_SECONDS = 20.0
READY_POLL_SECONDS = 0.5
SHUTDOWN_GRACE_SECONDS = 5.0

STATE_RUNNING = "running"
STATE_STARTING = "starting"
STATE_RECONNECTING = "reconnecting"
STATE_ERROR = "error"
STATE_STOPPED = "stopped"
STATE_MISSING = "missing"


class Go2rtcSupervisor:
    """Owns the go2rtc process and keeps its stream list in step with the DB."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        config_dir: Path,
        *,
        secret_key: str = "",
        binary: str = BINARY,
        log_level: str = "warn",
    ) -> None:
        self.conn = conn
        self.config_dir = Path(config_dir)
        self.secret_key = secret_key
        self.binary = binary
        self.log_level = log_level
        self.state = STATE_STOPPED
        self.detail = ""
        self.stderr: deque[str] = deque(maxlen=STDERR_LINES)
        self.backoff = backoff_mod.Backoff()
        self.process: asyncio.subprocess.Process | None = None
        self.started_at: float | None = None
        self.restarts = 0
        self.streams: list[str] = []
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    # ------------------------------------------------------------------ config

    @property
    def config_path(self) -> Path:
        return self.config_dir / "go2rtc.yaml"

    def _cameras(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM cameras ORDER BY sort_order, id"))

    def write_config(self) -> Path:
        """Write go2rtc.yaml from the current camera list.

        **This file contains camera credentials in its stream URLs**, because
        that is the only way go2rtc can authenticate to an RTSP source. It is
        created at mode 0600 before anything is written to it, so it is never
        briefly world-readable on the /config volume, which the operator
        mounts from the host.
        """
        document = config.build_config(
            self._cameras(), log_level=self.log_level, secret=self.secret_key
        )
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        # Create (or truncate) with the right mode first: write_text on an
        # existing file would keep whatever mode it already had.
        descriptor = os.open(self.config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(config.to_yaml(document))
        with contextlib.suppress(OSError):
            os.chmod(self.config_path, 0o600)
        self.streams = sorted(document["streams"])
        return self.config_path

    # ------------------------------------------------------------------ process

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    async def start(self) -> None:
        if not self.available():
            self.state = STATE_MISSING
            self.detail = (
                f"the `{self.binary}` binary is not on PATH, so live video is unavailable. "
                "It ships in the container image; a local run needs it installed."
            )
            log.warning("go2rtc not found: live video disabled")
            return
        self.write_config()
        self._task = asyncio.create_task(self._loop(), name="nvr-go2rtc")

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.state = STATE_ERROR
                self.detail = f"supervisor error: {exc}"
                log.exception("go2rtc supervisor raised")

            if self._stopping.is_set():
                break
            ran_for = (time.monotonic() - self.started_at) if self.started_at else 0.0
            delay = self.backoff.next_delay(ran_for)
            self.state = STATE_RECONNECTING
            self.detail = f"restarting in {delay:.0f}s (attempt {self.backoff.attempts})"
            log.warning("go2rtc: %s", self.detail)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)

        self.state = STATE_STOPPED
        self.detail = "supervisor stopped"

    async def _run_once(self) -> None:
        self.state = STATE_STARTING
        self.detail = "starting go2rtc"
        process = await asyncio.create_subprocess_exec(
            self.binary,
            "-config",
            str(self.config_path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self.process = process
        self.started_at = time.monotonic()
        stderr_task = asyncio.create_task(self._drain_stderr(process))

        try:
            if await self._wait_until_ready():
                self.state = STATE_RUNNING
                self.detail = f"{len(self.streams)} stream(s) registered"
                self.backoff.reset()
                # Never fatal. go2rtc refusing one stream is not go2rtc being
                # down, and raising here put the daemon into a restart loop
                # every two seconds for as long as the refusal lasted.
                await self.sync_quietly()
            await process.wait()
        finally:
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stderr_task
            self.process = None

        self.restarts += 1
        if not self._stopping.is_set():
            self.state = STATE_ERROR
            self.detail = f"go2rtc exited with code {process.returncode}"
            log.warning("go2rtc: %s", self.detail)

    async def _wait_until_ready(self) -> bool:
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._stopping.is_set():
                return False
            if self.process is not None and self.process.returncode is not None:
                return False
            try:
                await client.ping()
                return True
            except client.LiveUnavailable:
                self.detail = "httpx is not installed, so go2rtc cannot be managed"
                return False
            except client.LiveError:
                await asyncio.sleep(READY_POLL_SECONDS)
        self.detail = f"go2rtc did not answer its API within {READY_TIMEOUT_SECONDS:.0f}s"
        return False

    async def _drain_stderr(self, process) -> None:
        assert process.stderr is not None
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                # go2rtc prints the whole source URL on a failed connection,
                # and this buffer is shown on the Core page.
                self.stderr.append(rtsp.redact(text))

    # ------------------------------------------------------------------- public

    async def sync(self) -> dict:
        """Re-register every stream. Called on start and after a camera edit.

        The config file is rewritten first. go2rtc patches that file when it
        accepts a stream, so leaving it stale means it patches something that
        no longer describes the cameras - and an empty one cannot be patched
        at all. Rewriting costs a few hundred bytes and keeps the file right
        for the next start.
        """
        self.write_config()
        cameras = self._cameras()
        result = await client.sync_streams(cameras, secret=self.secret_key)
        self.streams = result["streams"]
        if self.state == STATE_RUNNING:
            self.detail = f"{len(self.streams)} stream(s) registered"
        return result

    async def sync_quietly(self) -> None:
        """Sync without letting a go2rtc problem break the caller's request.

        Used on start as well as after a camera edit: a stream go2rtc will
        not accept costs that one camera's live view, and nothing else.
        """
        if self.state not in (STATE_RUNNING,):
            return
        try:
            await self.sync()
        except (client.LiveError, client.LiveUnavailable) as exc:
            log.warning("stream sync failed: %s", exc)
            self.detail = f"live video partly unavailable: {rtsp.redact(str(exc))}"

    async def stop(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        process = self.process
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=SHUTDOWN_GRACE_SECONDS)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self.state = STATE_STOPPED
        self.detail = "supervisor stopped"

    def status(self) -> dict:
        return {
            "state": self.state,
            "detail": self.detail,
            "streams": list(self.streams),
            "restarts": self.restarts,
            "stderr_tail": list(self.stderr),
        }

    def health(self) -> tuple[bool, str, str]:
        """Live video health.

        Not fatal to the process: the recorder pulls RTSP straight from the
        cameras, so go2rtc being down costs live view and nothing else. The
        same reasoning as camera-level recorder faults, and `NightwireDown`
        plus the Core page are where this surfaces.
        """
        if self.state == STATE_RUNNING:
            return True, STATE_RUNNING, self.detail
        if self.state == STATE_MISSING:
            return True, STATE_MISSING, self.detail
        if not db.enabled_cameras(self.conn) and not self._cameras():
            return True, "idle", "no cameras configured"
        return True, self.state or STATE_STOPPED, self.detail or "live video is not running"
