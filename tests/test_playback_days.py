"""Which local days have footage, for the Rewind calendar.

Pure day arithmetic, so it is tested without a database. The cases that
matter are the ones a 24-hour-a-day recorder actually hits: a segment that
straddles local midnight, and a day that does not have 24 hours in it
because the clocks changed.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from nightwire.playback import timeline

EASTERN = ZoneInfo("America/New_York")


def at(text: str, tz=EASTERN) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=tz).timestamp() * 1000)


def segment(start_ms: int, seconds: int = 60, size: int = 8_000_000, ident: int = 1):
    return timeline.Segment(
        id=ident,
        start_ts=start_ms,
        end_ts=start_ms + seconds * 1000,
        duration_ms=seconds * 1000,
        path="x.ts",
        size_bytes=size,
    )


def test_no_segments_means_no_days():
    assert timeline.days_with_footage([], EASTERN) == []


def test_one_day_is_counted_once():
    segs = [segment(at("2026-10-30T14:00:00"), ident=i) for i in range(3)]
    [day] = timeline.days_with_footage(segs, EASTERN)
    assert day["date"] == "2026-10-30"
    assert day["segments"] == 3
    assert day["seconds"] == 180
    assert day["bytes"] == 24_000_000


def test_days_come_back_oldest_first():
    segs = [
        segment(at("2026-10-30T09:00:00"), ident=2),
        segment(at("2026-10-28T09:00:00"), ident=1),
        segment(at("2026-10-29T09:00:00"), ident=3),
    ]
    assert [d["date"] for d in timeline.days_with_footage(segs, EASTERN)] == [
        "2026-10-28",
        "2026-10-29",
        "2026-10-30",
    ]


def test_a_segment_belongs_to_the_day_it_starts():
    """One straddling local midnight must not be counted on both days."""
    segs = [segment(at("2026-10-30T23:59:30"), seconds=60)]
    [day] = timeline.days_with_footage(segs, EASTERN)
    assert day["date"] == "2026-10-30"
    assert day["segments"] == 1


def test_days_are_local_not_utc():
    """19:00 Eastern is the next day in UTC; the calendar shows local days."""
    segs = [segment(at("2026-10-30T20:00:00"))]
    [day] = timeline.days_with_footage(segs, EASTERN)
    assert day["date"] == "2026-10-30"

    from datetime import timezone as dt_timezone

    [utc_day] = timeline.days_with_footage(segs, dt_timezone.utc)
    assert utc_day["date"] == "2026-10-31"


def test_the_day_the_clocks_go_back_has_25_hours():
    """A full day of recording across a DST fall-back is still one day."""
    start = at("2026-11-01T00:00:00")
    segs = [segment(start + i * 3600_000, ident=i) for i in range(25)]
    days = timeline.days_with_footage(segs, EASTERN)
    assert days[0]["date"] == "2026-11-01"
    assert days[0]["segments"] == 25


def test_a_missing_duration_does_not_break_the_tally():
    broken = timeline.Segment(
        id=1, start_ts=at("2026-10-30T10:00:00"), end_ts=0, duration_ms=0, path="x.ts"
    )
    [day] = timeline.days_with_footage([broken], EASTERN)
    assert day["seconds"] == 0
    assert day["segments"] == 1


def test_local_day_formats_a_moment():
    assert timeline.local_day(at("2026-10-30T14:00:00"), EASTERN) == "2026-10-30"
