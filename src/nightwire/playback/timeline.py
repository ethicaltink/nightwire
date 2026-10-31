"""Merging segments into recorded ranges. Pure, standard library only.

The whole-day bar and the detail view both need the same thing: contiguous
footage collapsed into ranges, with the holes between them named. Doing it
here rather than in the browser keeps one definition of "a gap" and makes it
testable, which the spec asks for by name.

All times are unix milliseconds UTC. Local time is a display concern.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# Segment boundaries are never exact: ffprobe measures 60.024 s where the
# muxer intended 60. Anything under this is the same continuous recording.
GAP_TOLERANCE_MS = 1000

DAY_MS = 86_400_000


@dataclass(frozen=True)
class Segment:
    """Just enough of a `segments` row to build a timeline."""

    id: int
    start_ts: int
    end_ts: int
    duration_ms: int
    path: str
    size_bytes: int = 0
    has_audio: bool = False

    @property
    def effective_end(self) -> int:
        """End of playable video.

        A segment whose duration could not be measured is stored with
        duration 0 (see the indexer). Trusting `end_ts` blindly would give it
        zero width on the timeline and make it unseekable, so fall back to
        `end_ts` when it is larger.
        """
        return max(self.end_ts, self.start_ts + self.duration_ms)


@dataclass(frozen=True)
class Range:
    start_ts: int
    end_ts: int
    segment_count: int = 0

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ts - self.start_ts)

    def as_dict(self) -> dict:
        return {
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "duration_ms": self.duration_ms,
            "segments": self.segment_count,
        }


def day_bounds(date_text: str, tz=None) -> tuple[int, int]:
    """Local midnight to midnight for a `YYYY-MM-DD` date, in unix ms UTC.

    The UI asks for a day; the database stores UTC. A local day is not 24
    hours across a DST change, so the end is computed as the next local
    midnight rather than start plus 86,400,000.
    """
    day = datetime.strptime(date_text, "%Y-%m-%d")
    start = day.replace(tzinfo=tz) if tz is not None else day.astimezone()
    next_day = (day + timedelta(days=1)).replace(tzinfo=tz) if tz is not None else (
        (day + timedelta(days=1)).astimezone()
    )
    return int(start.timestamp() * 1000), int(next_day.timestamp() * 1000)


def local_day(at_ms: int, tz=None) -> str:
    """The `YYYY-MM-DD` a moment falls on, in the display time zone."""
    moment = datetime.fromtimestamp(at_ms / 1000, tz) if tz is not None else (
        datetime.fromtimestamp(at_ms / 1000)
    )
    return moment.strftime("%Y-%m-%d")


def days_with_footage(segments: list[Segment], tz=None) -> list[dict]:
    """Which local days have recordings, and how much.

    The calendar on Rewind uses this to offer only the days that exist, so
    picking a date cannot land on an empty page. A segment is counted on the
    day it **starts**: one that straddles local midnight belongs to the day
    it began, which keeps a day's totals from double-counting.

    Pure, so the day arithmetic is testable without a database. Returns
    oldest first: `[{"date", "segments", "bytes", "seconds"}]`.
    """
    tally: dict[str, dict] = {}
    for segment in segments:
        day = local_day(segment.start_ts, tz)
        entry = tally.setdefault(
            day, {"date": day, "segments": 0, "bytes": 0, "seconds": 0.0}
        )
        entry["segments"] += 1
        entry["bytes"] += getattr(segment, "size_bytes", 0) or 0
        entry["seconds"] += (segment.duration_ms or 0) / 1000
    return [
        {**entry, "seconds": round(entry["seconds"])}
        for _, entry in sorted(tally.items())
    ]


def overlapping(segments: list[Segment], start_ms: int, end_ms: int) -> list[Segment]:
    """Segments that overlap a window at all, in chronological order.

    Overlap, not containment: a segment that starts before the window and
    runs into it carries the first frames the user asked for.
    """
    return sorted(
        (s for s in segments if s.effective_end > start_ms and s.start_ts < end_ms),
        key=lambda s: s.start_ts,
    )


def merge(segments: list[Segment], tolerance_ms: int = GAP_TOLERANCE_MS) -> list[Range]:
    """Collapse contiguous segments into ranges."""
    ordered = sorted(segments, key=lambda s: s.start_ts)
    ranges: list[Range] = []
    start = end = None
    count = 0

    for segment in ordered:
        if start is None:
            start, end, count = segment.start_ts, segment.effective_end, 1
            continue
        if segment.start_ts - end <= tolerance_ms:
            # Overlapping or touching: extend, never shrink.
            end = max(end, segment.effective_end)
            count += 1
            continue
        ranges.append(Range(start, end, count))
        start, end, count = segment.start_ts, segment.effective_end, 1

    if start is not None:
        ranges.append(Range(start, end, count))
    return ranges


def gaps(ranges: list[Range], start_ms: int, end_ms: int) -> list[Range]:
    """The holes between ranges, clipped to the window.

    Includes the leading and trailing holes, so the bar can distinguish
    "nothing recorded yet" from "recorded and then stopped".
    """
    holes: list[Range] = []
    cursor = start_ms
    for recorded in sorted(ranges, key=lambda r: r.start_ts):
        if recorded.start_ts > cursor:
            holes.append(Range(cursor, min(recorded.start_ts, end_ms)))
        cursor = max(cursor, recorded.end_ts)
        if cursor >= end_ms:
            break
    if cursor < end_ms:
        holes.append(Range(cursor, end_ms))
    return [hole for hole in holes if hole.duration_ms > 0]


def coverage_ms(ranges: list[Range], start_ms: int, end_ms: int) -> int:
    """How much of the window has footage, counting only the part inside it."""
    total = 0
    for recorded in ranges:
        overlap_start = max(recorded.start_ts, start_ms)
        overlap_end = min(recorded.end_ts, end_ms)
        if overlap_end > overlap_start:
            total += overlap_end - overlap_start
    return total


def build(
    segments: list[Segment],
    start_ms: int,
    end_ms: int,
    *,
    tolerance_ms: int = GAP_TOLERANCE_MS,
    now_ms: int | None = None,
) -> dict:
    """The payload behind `GET /api/cameras/{id}/timeline`.

    `not_yet` separates the future part of today from a real gap: a bar for
    today should not paint the evening as missing footage.
    """
    window = overlapping(segments, start_ms, end_ms)
    ranges = merge(window, tolerance_ms)
    holes = gaps(ranges, start_ms, end_ms)

    not_yet = None
    if now_ms is not None and start_ms <= now_ms < end_ms:
        not_yet = Range(now_ms, end_ms)
        holes = [
            Range(hole.start_ts, min(hole.end_ts, now_ms))
            for hole in holes
            if hole.start_ts < now_ms
        ]
        holes = [hole for hole in holes if hole.duration_ms > 0]

    covered = coverage_ms(ranges, start_ms, end_ms)
    measurable = (not_yet.start_ts if not_yet else end_ms) - start_ms
    return {
        "start_ts": start_ms,
        "end_ts": end_ms,
        "ranges": [r.as_dict() for r in ranges],
        "gaps": [g.as_dict() for g in holes],
        "not_yet": not_yet.as_dict() if not_yet else None,
        "segment_count": len(window),
        "covered_ms": covered,
        "coverage_percent": round(covered / measurable * 100, 2) if measurable > 0 else 0.0,
    }


def nearest_segment(segments: list[Segment], at_ms: int) -> Segment | None:
    """The segment containing `at_ms`, or the closest one either side.

    Used by the hover thumbnail, where the user's pointer rarely lands inside
    recorded footage exactly.
    """
    if not segments:
        return None
    for segment in segments:
        if segment.start_ts <= at_ms < segment.effective_end:
            return segment
    return min(
        segments,
        key=lambda s: min(abs(s.start_ts - at_ms), abs(s.effective_end - at_ms)),
    )
