"""One ONVIF PullPoint subscription per camera.

The shape is the recorder's: one asyncio task per camera, the same backoff,
and a status the UI reads. What it adds is the event lifecycle, which lives
in `tracker.py` so it can be tested without a camera.

The rules it enforces, from 1. Subscribe, filtered to the chosen topics, with a 10 minute termination.
2. Pull with a 10 s timeout and a limit of 50, renewing at half the
   termination time.
3. On error or camera reboot, back off 2 s to 60 s and resubscribe. A new
   subscription receives the current state of every stateful topic, so an
   `on` at subscribe time opens an event, and anything left open across the
   reconnect is closed at the reconnect time.
4. Report `subscribed`, `reconnecting` or `no sources`, plus the last event
   time, for Core and the Nodes page.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from .. import cameras as repo
from ..live import client as live_client
from ..live import config as live_config
from ..onvif import client as onvif
from ..recording import backoff as backoff_mod
from ..recording import paths
from . import clock as clock_mod
from . import messages as messages_mod
from . import store
from . import tracker as tracker_mod
from .bus import bus

log = logging.getLogger("nightwire.events.listener")

PULL_TIMEOUT_SECONDS = 10
PULL_LIMIT = 50
TERMINATION_SECONDS = 600
RENEW_EVERY_SECONDS = TERMINATION_SECONDS // 2
CAP_TICK_SECONDS = 30
ERROR_LINES = 20

STATE_SUBSCRIBED = "subscribed"
STATE_RECONNECTING = "reconnecting"
STATE_NO_SOURCES = "no sources"
STATE_STOPPED = "stopped"
STATE_UNAVAILABLE = "unavailable"


class CameraListener:
    """Holds one camera's subscription state."""

    def __init__(self, camera_id: str, name: str) -> None:
        self.camera_id = camera_id
        self.name = name
        self.state = STATE_STOPPED
        self.detail = ""
        self.tracker = tracker_mod.EventTracker()
        self.backoff = backoff_mod.Backoff()
        self.errors: deque[str] = deque(maxlen=ERROR_LINES)
        self.last_event_ms: int | None = None
        self.event_count = 0
        self.resubscribes = 0
        self.subscribed_at: float | None = None

    def note(self, line: str) -> None:
        if line:
            self.errors.append(f"{datetime.now().strftime('%H:%M:%S')} {line}")

    def status(self) -> dict:
        return {
            "state": self.state,
            "detail": self.detail,
            "last_event_at": (
                None
                if self.last_event_ms is None
                else datetime.fromtimestamp(self.last_event_ms / 1000, timezone.utc).isoformat()
            ),
            "last_event_ms": self.last_event_ms,
            "events_seen": self.event_count,
            "resubscribes": self.resubscribes,
            "open_sources": self.tracker.open_sources(),
            "errors": list(self.errors),
        }


class EventSupervisor:
    """Starts, watches and stops every camera's event listener."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        recordings_dir: Path,
        *,
        secret_key: str,
        snapshots: bool = True,
    ) -> None:
        self.conn = conn
        self.recordings_dir = Path(recordings_dir)
        self.secret_key = secret_key
        self.snapshots = snapshots
        self.listeners: dict[str, CameraListener] = {}
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()
        self.started = False

    # ------------------------------------------------------------------ setup

    def _cameras(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM cameras ORDER BY sort_order, id"))

    async def start(self) -> None:
        now_ms = int(time.time() * 1000)
        for row in self._cameras():
            # A crash can leave events open with no end; close them before
            # anything new opens, or a block grows across the timeline forever.
            store.close_dangling(self.conn, row["id"], now_ms)
            listener = CameraListener(row["id"], row["name"])
            self.listeners[row["id"]] = listener
            self._tasks.append(
                asyncio.create_task(self._camera_loop(listener), name=f"nvr-evt-{row['id']}")
            )
        self._tasks.append(asyncio.create_task(self._cap_loop(), name="nvr-evt-cap"))
        self._tasks.append(asyncio.create_task(self._clock_loop(), name="nvr-evt-clock"))
        self.started = True
        log.info("event supervisor started with %d camera(s)", len(self.listeners))

    # ------------------------------------------------------------------- loops

    async def _camera_loop(self, listener: CameraListener) -> None:
        while not self._stopping.is_set():
            sources = store.enabled_sources(self.conn, listener.camera_id)
            if not sources:
                listener.state = STATE_NO_SOURCES
                listener.detail = (
                    "No event sources chosen. Pick them on Nodes after rescanning the camera."
                )
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=30)
                continue

            try:
                await self._subscribe_and_pull(listener, sources)
            except asyncio.CancelledError:
                raise
            except onvif.OnvifUnavailable as exc:
                listener.state = STATE_UNAVAILABLE
                listener.detail = str(exc)
                listener.note(str(exc))
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=60)
                continue
            except Exception as exc:  # noqa: BLE001 - a listener must not die
                listener.note(f"subscription failed: {exc}")
                log.warning("events %s: %s", listener.camera_id, exc)

            if self._stopping.is_set():
                break

            # Whatever happened, the subscription is gone: close anything the
            # camera had open, at the moment we lost it.
            self._apply(listener, listener.tracker.on_reconnect(int(time.time() * 1000)))

            ran_for = (time.monotonic() - listener.subscribed_at) if listener.subscribed_at else 0.0
            delay = listener.backoff.next_delay(ran_for)
            listener.resubscribes += 1
            listener.state = STATE_RECONNECTING
            listener.detail = f"resubscribing in {delay:.0f}s (attempt {listener.backoff.attempts})"
            listener.note(listener.detail)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)

        listener.state = STATE_STOPPED
        listener.detail = "supervisor stopped"

    async def _subscribe_and_pull(self, listener: CameraListener, sources: list[dict]) -> None:
        row = repo.get_row(self.conn, listener.camera_id)
        username, password = repo.credentials(self.conn, listener.camera_id, self.secret_key)
        topics = sorted({source["topic"] for source in sources})

        point = onvif.PullPoint(row["host"], row["onvif_port"], username, password)
        await point.connect(topics)
        listener.subscribed_at = time.monotonic()
        listener.state = STATE_SUBSCRIBED
        listener.detail = f"{len(sources)} source(s) subscribed"
        listener.backoff.reset()
        log.info("events %s: subscribed to %d topic(s)", listener.camera_id, len(topics))

        last_renew = time.monotonic()
        try:
            while not self._stopping.is_set():
                notifications = await point.pull(PULL_TIMEOUT_SECONDS, PULL_LIMIT)
                if notifications:
                    self._handle(listener, notifications)
                if time.monotonic() - last_renew >= RENEW_EVERY_SECONDS:
                    await point.renew()
                    last_renew = time.monotonic()
        finally:
            await point.close()

    async def _cap_loop(self) -> None:
        """Enforce the 10-minute cap on anything left open."""
        while not self._stopping.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=CAP_TICK_SECONDS)
            if self._stopping.is_set():
                return
            now_ms = int(time.time() * 1000)
            for listener in list(self.listeners.values()):
                effects = listener.tracker.tick(now_ms)
                if effects:
                    listener.note("10-minute cap reached: closing and reopening")
                    self._apply(listener, effects)

    async def _clock_loop(self) -> None:
        """Measure each camera's clock offset, hourly."""
        while not self._stopping.is_set():
            for camera_id in list(self.listeners):
                if self._stopping.is_set():
                    return
                await self.measure_clock(camera_id)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=clock_mod.REFRESH_SECONDS
                )

    async def measure_clock(self, camera_id: str) -> clock_mod.Offset | None:
        """Read one camera's clock and store the offset."""
        listener = self.listeners.get(camera_id)
        try:
            row = repo.get_row(self.conn, camera_id)
            username, password = repo.credentials(self.conn, camera_id, self.secret_key)
            camera_ms, sent, received = await onvif.system_time_ms(
                row["host"], row["onvif_port"], username, password
            )
        except Exception as exc:  # noqa: BLE001 - a clock read is never fatal
            log.debug("clock read failed for %s: %s", camera_id, exc)
            return None

        offset = clock_mod.measure(camera_id, camera_ms, sent, received)
        store.save_offset(self.conn, offset)
        if offset.warn:
            log.warning("camera clock: %s", offset.detail)
            if listener is not None:
                listener.note(offset.detail)
        return offset

    # ---------------------------------------------------------------- handling

    def _handle(self, listener: CameraListener, notifications: list[dict]) -> None:
        index = store.source_index(self.conn, listener.camera_id)
        offset = store.load_offset(self.conn, listener.camera_id)

        for raw in notifications:
            notification = messages_mod.parse(raw)
            if notification is None:
                continue
            source = messages_mod.match(notification, index)
            if source is None:
                continue

            camera_ts = notification.camera_ts_ms
            # Correct onto the NVR clock; keep the raw camera time for the row.
            at_ms = (
                clock_mod.correct(camera_ts, offset)
                if camera_ts is not None
                else int(time.time() * 1000)
            )

            listener.last_event_ms = at_ms
            listener.event_count += 1

            if source["kind"] == "point" or not notification.is_stateful:
                effects = listener.tracker.on_point(source["id"], at_ms)
            else:
                effects = listener.tracker.on_value(source["id"], notification.value, at_ms)

            # Applied per notification, not per batch. A camera routinely
            # delivers an on and its matching off in one PullMessages
            # response; batching would compute the close before the open had
            # been inserted, so the tracker would not know the row id yet and
            # the close would be silently dropped, leaving the event open
            # forever.
            if effects:
                self._apply(
                    listener,
                    effects,
                    {source["id"]: camera_ts} if camera_ts is not None else None,
                )

    def _apply(
        self,
        listener: CameraListener,
        effects: list,
        camera_ts_by_source: dict[int, int] | None = None,
    ) -> None:
        changed = store.apply_effects(
            self.conn,
            listener.camera_id,
            effects,
            listener.tracker,
            camera_ts_by_source=camera_ts_by_source,
        )
        for change in changed:
            event = store.get_event(self.conn, change["event_id"])
            if event is None:
                continue
            bus.publish({"type": change["action"], "camera_id": listener.camera_id, "event": event})
            if change["action"] in ("open", "point") and self.snapshots:
                asyncio.create_task(self._snapshot(listener.camera_id, change["event_id"]))

    async def _snapshot(self, camera_id: str, event_id: int) -> None:
        """Save a still for an event, through go2rtc's frame API.

        Best effort: a missing snapshot costs a hover preview, not an event.
        """
        try:
            camera = repo.get(self.conn, camera_id)
            image = await live_client.frame(live_config.grid_stream_for(camera))
        except Exception as exc:  # noqa: BLE001
            log.debug("event snapshot failed for %s/%s: %s", camera_id, event_id, exc)
            return

        directory = paths.events_dir(self.recordings_dir, camera_id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{event_id}.jpg"
        try:
            target.write_bytes(image)
        except OSError as exc:
            log.warning("could not write event snapshot %s: %s", target, exc)
            return
        store.record_snapshot(
            self.conn, event_id, paths.relative_path(self.recordings_dir, target)
        )

    # ----------------------------------------------------------------- control

    async def resubscribe(self, camera_id: str) -> None:
        """Drop a camera's subscription so its loop picks up new sources."""
        listener = self.listeners.get(camera_id)
        if listener is None:
            return
        listener.backoff.reset()
        listener.note("sources changed: resubscribing")
        # The loop re-reads sources each time round, so closing the open
        # events and letting the pull fail is enough.
        self._apply(listener, listener.tracker.on_reconnect(int(time.time() * 1000)))

    async def stop(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        now_ms = int(time.time() * 1000)
        for listener in self.listeners.values():
            self._apply(listener, listener.tracker.on_reconnect(now_ms))
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for listener in self.listeners.values():
            listener.state = STATE_STOPPED
            listener.detail = "supervisor stopped"
        self.started = False
        log.info("event supervisor stopped")

    # ------------------------------------------------------------------ status

    def status(self, camera_id: str) -> dict:
        listener = self.listeners.get(camera_id)
        if listener is None:
            return {
                "state": STATE_STOPPED,
                "detail": "No event listener for this camera.",
                "last_event_at": None,
                "errors": [],
            }
        return listener.status()

    def health(self) -> tuple[bool, str, str]:
        """Event listener health.

        Like the recorder and go2rtc: a camera-level fault is reported, not
        escalated into a container restart.
        """
        if not self.started:
            return True, "stopped", "the event listener is not running"
        total = len(self.listeners)
        if total == 0:
            return True, "idle", "no cameras configured"
        subscribed = sum(1 for l in self.listeners.values() if l.state == STATE_SUBSCRIBED)
        no_sources = sum(1 for l in self.listeners.values() if l.state == STATE_NO_SOURCES)
        if no_sources == total:
            return True, STATE_NO_SOURCES, "no event sources chosen on any camera"
        detail = f"{subscribed} of {total} subscribed"
        if subscribed < total - no_sources:
            faulty = sorted(
                f"{l.camera_id} ({l.state})"
                for l in self.listeners.values()
                if l.state not in (STATE_SUBSCRIBED, STATE_NO_SOURCES)
            )
            detail += f"; attention: {', '.join(faulty)}"
            return True, "degraded", detail
        return True, STATE_SUBSCRIBED, detail
