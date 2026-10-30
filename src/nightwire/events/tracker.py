"""The event lifecycle: open, close, merge, cap, reconnect. Pure.

This is where every rule from lives, with no database
and no camera, so all of it is testable:

- `true` opens an event, `false` closes it.
- Events on the same source less than 10 s apart are merged, so a camera
  flickering on and off does not produce twenty markers for one person
  walking past.
- An open event is capped at 10 minutes, then closed and reopened, so a
  stuck `true` cannot paint the whole day as motion.
- A new subscription receives the current state of every stateful event, so
  an `on` at subscribe time opens an event; anything left open across a
  reconnect closes at the reconnect time rather than hanging forever.

The tracker emits **effects** rather than writing anything. `store.py`
applies them. That split is what makes the rules testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MERGE_WINDOW_MS = 10_000
MAX_OPEN_MS = 600_000  # 10 minutes


@dataclass(frozen=True)
class Open:
    """Start a new event."""

    source_id: int
    start_ts: int
    camera_ts: int | None = None


@dataclass(frozen=True)
class Close:
    """Finish an open event."""

    event_id: int
    source_id: int
    end_ts: int


@dataclass(frozen=True)
class Reopen:
    """Clear the end time of a just-closed event, merging the two."""

    event_id: int
    source_id: int


@dataclass(frozen=True)
class Point:
    """A one-shot event, which has no duration."""

    source_id: int
    start_ts: int
    camera_ts: int | None = None


@dataclass
class _OpenState:
    event_id: int | None
    start_ts: int


@dataclass
class _ClosedState:
    event_id: int
    end_ts: int


@dataclass
class EventTracker:
    """Per-camera open/closed state for every event source."""

    merge_window_ms: int = MERGE_WINDOW_MS
    max_open_ms: int = MAX_OPEN_MS
    _open: dict[int, _OpenState] = field(default_factory=dict)
    _closed: dict[int, _ClosedState] = field(default_factory=dict)

    # ------------------------------------------------------------- queries

    def is_open(self, source_id: int) -> bool:
        return source_id in self._open

    def open_sources(self) -> list[int]:
        return sorted(self._open)

    def open_since(self, source_id: int) -> int | None:
        state = self._open.get(source_id)
        return None if state is None else state.start_ts

    def note_event_id(self, source_id: int, event_id: int) -> None:
        """Tell the tracker the row id the store assigned to an Open."""
        state = self._open.get(source_id)
        if state is not None and state.event_id is None:
            state.event_id = event_id

    # ------------------------------------------------------------- the rules

    def on_value(self, source_id: int, value: bool, at_ms: int) -> list:
        """A stateful topic reported `value` at `at_ms`."""
        if value:
            return self._begin(source_id, at_ms)
        return self._end(source_id, at_ms)

    def _begin(self, source_id: int, at_ms: int) -> list:
        if source_id in self._open:
            # Already on; a repeated `true` is not a second event.
            return []
        recent = self._closed.get(source_id)
        if recent is not None and at_ms - recent.end_ts <= self.merge_window_ms:
            # Close enough to be the same incident: reopen rather than
            # starting a second marker.
            self._open[source_id] = _OpenState(event_id=recent.event_id, start_ts=at_ms)
            del self._closed[source_id]
            return [Reopen(event_id=recent.event_id, source_id=source_id)]
        self._open[source_id] = _OpenState(event_id=None, start_ts=at_ms)
        return [Open(source_id=source_id, start_ts=at_ms)]

    def _end(self, source_id: int, at_ms: int) -> list:
        state = self._open.pop(source_id, None)
        if state is None:
            # A `false` with nothing open: normal right after subscribing,
            # because the camera reports the current state of everything.
            return []
        end_ts = max(at_ms, state.start_ts)
        if state.event_id is not None:
            self._closed[source_id] = _ClosedState(event_id=state.event_id, end_ts=end_ts)
            return [Close(event_id=state.event_id, source_id=source_id, end_ts=end_ts)]
        # The store never told us an id, so there is nothing to close.
        return []

    def on_point(self, source_id: int, at_ms: int) -> list:
        """A one-shot topic fired. No open state, no merging."""
        return [Point(source_id=source_id, start_ts=at_ms)]

    def tick(self, now_ms: int) -> list:
        """Enforce the 10-minute cap on anything still open."""
        effects: list = []
        for source_id in sorted(self._open):
            state = self._open[source_id]
            if now_ms - state.start_ts < self.max_open_ms:
                continue
            if state.event_id is None:
                # Nothing to close yet; move the window so it is reconsidered.
                state.start_ts = now_ms
                continue
            effects.append(Close(event_id=state.event_id, source_id=source_id, end_ts=now_ms))
            # Reopen immediately: the camera still says motion, so the
            # timeline should show a new block starting now rather than a gap.
            self._open[source_id] = _OpenState(event_id=None, start_ts=now_ms)
            effects.append(Open(source_id=source_id, start_ts=now_ms))
            self._closed.pop(source_id, None)
        return effects

    def on_reconnect(self, at_ms: int) -> list:
        """Close everything still open, at the moment the connection dropped.

        Without this a camera reboot leaves an event open forever, and the
        spec's Done-when requires that nothing is left stuck open.
        """
        effects: list = []
        for source_id in sorted(self._open):
            state = self._open[source_id]
            if state.event_id is not None:
                end_ts = max(at_ms, state.start_ts)
                effects.append(Close(event_id=state.event_id, source_id=source_id, end_ts=end_ts))
        self._open.clear()
        # After a reconnect the merge window is meaningless: the camera will
        # resend current state, and merging against a pre-outage close would
        # stitch an event across the gap.
        self._closed.clear()
        return effects

    def forget(self, source_id: int) -> None:
        """Drop state for a source the operator has disabled or deleted."""
        self._open.pop(source_id, None)
        self._closed.pop(source_id, None)
