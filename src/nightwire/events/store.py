"""Event sources and events in SQLite, and applying the tracker's effects.

The lifecycle rules live in `tracker.py`; this module only persists what the
tracker decides. Keeping them apart is what lets the rules be tested without
a database.
"""

from __future__ import annotations

import logging
import re
import sqlite3

from . import topics as topics_mod
from . import tracker as tracker_mod
from .clock import Offset, correct

log = logging.getLogger("nightwire.events")

# Default lane colours, in the order they are handed out. Taken from the
# spec's Look table so a new source never lands on an unthemed colour.
DEFAULT_COLORS = ("#FF2BD6", "#FF8A1E", "#C9A6FF", "#39FF88", "#FCEE0A", "#7DF9FF")


class EventError(ValueError):
    """Bad input for an event source."""


# --------------------------------------------------------------- event sources


def list_sources(conn: sqlite3.Connection, camera_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM event_sources WHERE camera_id = ? ORDER BY id", (camera_id,)
    )
    return [dict(row) for row in rows]


def enabled_sources(conn: sqlite3.Connection, camera_id: str) -> list[dict]:
    return [source for source in list_sources(conn, camera_id) if source["enabled"]]


def source_index(conn: sqlite3.Connection, camera_id: str) -> dict[str, dict]:
    """Enabled sources keyed by `topic#source_filter`, for matching messages."""
    return {
        topics_mod.source_key(source["topic"], source["source_filter"]): source
        for source in enabled_sources(conn, camera_id)
    }


def next_color(existing: list[dict]) -> str:
    used = {(source.get("color") or "").upper() for source in existing}
    for color in DEFAULT_COLORS:
        if color.upper() not in used:
            return color
    return DEFAULT_COLORS[len(existing) % len(DEFAULT_COLORS)]


# A source colour is written straight into a `style=` attribute in four Svelte
# components, so it is validated here rather than escaped at each of them: a
# six-digit hex colour and nothing else. Anything else is a request to inject
# CSS, and the one legitimate source of these values is the colour picker.
COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")


def validate_color(value: str) -> str:
    text = (value or "").strip()
    if not COLOR_PATTERN.match(text):
        raise EventError(
            f"A colour must be six hex digits after a #, like #4ea1ff. Got {value!r}."
        )
    return text


def replace_sources(conn: sqlite3.Connection, camera_id: str, wanted: list[dict]) -> list[dict]:
    """Set the chosen event sources for a camera.

    Sources the operator unticked are **disabled, not deleted**, because
    events already on the timeline reference them and deleting the row would
    orphan a day's markers. A source with no events left is removed.
    """
    for entry in wanted:
        if not entry.get("topic"):
            raise EventError("Every event source needs a topic.")
        if not (entry.get("label") or "").strip():
            raise EventError("Every event source needs a label.")
        if entry.get("kind") not in (topics_mod.SPAN, topics_mod.POINT):
            raise EventError(
                f"kind must be {topics_mod.SPAN!r} or {topics_mod.POINT!r}, "
                f"got {entry.get('kind')!r}."
            )

    existing = {
        topics_mod.source_key(row["topic"], row["source_filter"]): row
        for row in list_sources(conn, camera_id)
    }
    seen: set[str] = set()

    for entry in wanted:
        key = topics_mod.source_key(entry["topic"], entry.get("source_filter"))
        seen.add(key)
        given = (entry.get("color") or "").strip()
        color = validate_color(given) if given else next_color(list_sources(conn, camera_id))
        if key in existing:
            conn.execute(
                "UPDATE event_sources SET label = ?, color = ?, kind = ?, enabled = 1"
                " WHERE id = ?",
                (entry["label"].strip(), color, entry["kind"], existing[key]["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO event_sources (camera_id, topic, source_filter, label, color, kind,"
                " enabled) VALUES (?,?,?,?,?,?,1)",
                (
                    camera_id,
                    entry["topic"],
                    entry.get("source_filter") or None,
                    entry["label"].strip(),
                    color,
                    entry["kind"],
                ),
            )

    for key, row in existing.items():
        if key in seen:
            continue
        used = conn.execute(
            "SELECT COUNT(*) FROM events WHERE source_id = ?", (row["id"],)
        ).fetchone()[0]
        if used:
            conn.execute("UPDATE event_sources SET enabled = 0 WHERE id = ?", (row["id"],))
        else:
            conn.execute("DELETE FROM event_sources WHERE id = ?", (row["id"],))

    conn.commit()
    return list_sources(conn, camera_id)


# ----------------------------------------------------------------- the effects


def apply_effects(
    conn: sqlite3.Connection,
    camera_id: str,
    effects: list,
    tracker: tracker_mod.EventTracker,
    *,
    camera_ts_by_source: dict[int, int] | None = None,
) -> list[dict]:
    """Persist what the tracker decided. Returns rows that changed, for SSE."""
    camera_ts_by_source = camera_ts_by_source or {}
    changed: list[dict] = []

    for effect in effects:
        if isinstance(effect, tracker_mod.Open):
            cursor = conn.execute(
                "INSERT INTO events (camera_id, source_id, start_ts, camera_ts) VALUES (?,?,?,?)",
                (
                    camera_id,
                    effect.source_id,
                    effect.start_ts,
                    camera_ts_by_source.get(effect.source_id),
                ),
            )
            event_id = int(cursor.lastrowid)
            tracker.note_event_id(effect.source_id, event_id)
            changed.append({"action": "open", "event_id": event_id, "source_id": effect.source_id})

        elif isinstance(effect, tracker_mod.Point):
            cursor = conn.execute(
                "INSERT INTO events (camera_id, source_id, start_ts, end_ts, camera_ts)"
                " VALUES (?,?,?,?,?)",
                (
                    camera_id,
                    effect.source_id,
                    effect.start_ts,
                    effect.start_ts,
                    camera_ts_by_source.get(effect.source_id),
                ),
            )
            changed.append(
                {"action": "point", "event_id": int(cursor.lastrowid), "source_id": effect.source_id}
            )

        elif isinstance(effect, tracker_mod.Close):
            conn.execute(
                "UPDATE events SET end_ts = ? WHERE id = ?", (effect.end_ts, effect.event_id)
            )
            changed.append(
                {"action": "close", "event_id": effect.event_id, "source_id": effect.source_id}
            )

        elif isinstance(effect, tracker_mod.Reopen):
            conn.execute("UPDATE events SET end_ts = NULL WHERE id = ?", (effect.event_id,))
            changed.append(
                {"action": "open", "event_id": effect.event_id, "source_id": effect.source_id}
            )

    if effects:
        conn.commit()
    return changed


def record_snapshot(conn: sqlite3.Connection, event_id: int, relative_path: str) -> None:
    conn.execute("UPDATE events SET snapshot = ? WHERE id = ?", (relative_path, event_id))
    conn.commit()


def close_dangling(conn: sqlite3.Connection, camera_id: str, at_ms: int) -> int:
    """Close events left open by a crash, at startup.

    The tracker handles a reconnect it witnessed. This handles the case where
    the process died with events open: without it, a restart leaves a block
    growing across the timeline forever.
    """
    rows = list(
        conn.execute(
            "SELECT id, start_ts FROM events WHERE camera_id = ? AND end_ts IS NULL",
            (camera_id,),
        )
    )
    for row in rows:
        conn.execute(
            "UPDATE events SET end_ts = ? WHERE id = ?",
            (max(at_ms, row["start_ts"]), row["id"]),
        )
    if rows:
        conn.commit()
        log.info("closed %d event(s) left open by a restart on %s", len(rows), camera_id)
    return len(rows)


# -------------------------------------------------------------------- reading


def _event_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    start = row["start_ts"]
    end = row["end_ts"]
    data["open"] = end is None
    data["duration_ms"] = None if end is None else max(0, end - start)
    return data


def events_for_day(
    conn: sqlite3.Connection,
    camera_id: str,
    start_ms: int,
    end_ms: int,
    *,
    source_ids: list[int] | None = None,
) -> list[dict]:
    """Events overlapping a window, oldest first, with their source details.

    An open event (`end_ts IS NULL`) is treated as running to the end of the
    window, so motion in progress still appears on today's timeline.
    """
    query = """
        SELECT e.*, s.label, s.color, s.kind, s.topic, s.source_filter
        FROM events e JOIN event_sources s ON s.id = e.source_id
        WHERE e.camera_id = ?
          AND e.start_ts < ?
          AND (e.end_ts IS NULL OR e.end_ts > ?)
    """
    params: list = [camera_id, end_ms, start_ms]
    if source_ids:
        placeholders = ",".join("?" for _ in source_ids)
        query += f" AND e.source_id IN ({placeholders})"
        params.extend(source_ids)
    query += " ORDER BY e.start_ts"
    return [_event_dict(row) for row in conn.execute(query, params)]


def recent(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Latest events across every camera, newest first. The Feeds strip."""
    rows = conn.execute(
        """
        SELECT e.*, s.label, s.color, s.kind, c.name AS camera_name
        FROM events e
        JOIN event_sources s ON s.id = e.source_id
        JOIN cameras c ON c.id = e.camera_id
        ORDER BY e.start_ts DESC
        LIMIT ?
        """,
        (max(1, min(int(limit), 100)),),
    )
    return [_event_dict(row) for row in rows]


def get_event(conn: sqlite3.Connection, event_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT e.*, s.label, s.color, s.kind, c.name AS camera_name
        FROM events e
        JOIN event_sources s ON s.id = e.source_id
        JOIN cameras c ON c.id = e.camera_id
        WHERE e.id = ?
        """,
        (event_id,),
    ).fetchone()
    return None if row is None else _event_dict(row)


def open_now(conn: sqlite3.Connection) -> list[dict]:
    """Every event currently open, for the live glow after a page reload."""
    rows = conn.execute(
        """
        SELECT e.*, s.label, s.color, s.kind, c.name AS camera_name
        FROM events e
        JOIN event_sources s ON s.id = e.source_id
        JOIN cameras c ON c.id = e.camera_id
        WHERE e.end_ts IS NULL
        ORDER BY e.start_ts
        """
    )
    return [_event_dict(row) for row in rows]


# --------------------------------------------------------------- clock offsets


def save_offset(conn: sqlite3.Connection, offset: Offset) -> None:
    conn.execute(
        "UPDATE cameras SET clock_offset_ms = ?, clock_checked_at = ? WHERE id = ?",
        (offset.offset_ms, offset.measured_at_ms, offset.camera_id),
    )
    conn.commit()


def load_offset(conn: sqlite3.Connection, camera_id: str) -> Offset | None:
    row = conn.execute(
        "SELECT clock_offset_ms, clock_checked_at FROM cameras WHERE id = ?", (camera_id,)
    ).fetchone()
    if row is None or row["clock_offset_ms"] is None:
        return None
    return Offset(
        camera_id=camera_id,
        offset_ms=int(row["clock_offset_ms"]),
        measured_at_ms=int(row["clock_checked_at"] or 0),
    )


def corrected_time(conn: sqlite3.Connection, camera_id: str, camera_ts_ms: int) -> int:
    return correct(camera_ts_ms, load_offset(conn, camera_id))
