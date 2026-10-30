"""Timeline merging. The spec asks for this by name."""

from datetime import timezone

from nightwire.playback import timeline
from nightwire.playback.timeline import Range, Segment

MIN = 60_000
HOUR = 60 * MIN
DAY = 24 * HOUR
T0 = 1_793_000_000_000  # an arbitrary but fixed moment


def seg(index, start, duration=MIN):
    return Segment(
        id=index,
        start_ts=start,
        end_ts=start + duration,
        duration_ms=duration,
        path=f"front/{index}.ts",
    )


def run(count, start=T0, duration=MIN, step=None):
    """`count` back-to-back segments."""
    step = duration if step is None else step
    return [seg(i, start + i * step, duration) for i in range(count)]


# ---------------------------------------------------------------- effective end


def test_a_zero_duration_segment_still_has_width():
    """The indexer stores 0 when ffprobe could not measure a segment."""
    broken = Segment(id=1, start_ts=T0, end_ts=T0 + MIN, duration_ms=0, path="x.ts")
    assert broken.effective_end == T0 + MIN


def test_duration_wins_when_it_is_larger():
    odd = Segment(id=1, start_ts=T0, end_ts=T0 + 100, duration_ms=MIN, path="x.ts")
    assert odd.effective_end == T0 + MIN


# ---------------------------------------------------------------------- merge


def test_contiguous_segments_become_one_range():
    ranges = timeline.merge(run(10))
    assert len(ranges) == 1
    assert ranges[0].start_ts == T0
    assert ranges[0].end_ts == T0 + 10 * MIN
    assert ranges[0].segment_count == 10


def test_a_real_gap_splits_the_range():
    segments = run(3) + run(3, start=T0 + 10 * MIN)
    ranges = timeline.merge(segments)
    assert len(ranges) == 2
    assert ranges[0].end_ts == T0 + 3 * MIN
    assert ranges[1].start_ts == T0 + 10 * MIN


def test_sub_second_jitter_is_not_a_gap():
    """ffprobe measures 60.024 s where the muxer intended 60."""
    segments = [seg(0, T0, 60_024), seg(1, T0 + 60_500, 59_900)]
    assert len(timeline.merge(segments)) == 1


def test_the_gap_tolerance_is_configurable():
    segments = [seg(0, T0, MIN), seg(1, T0 + MIN + 3000)]
    assert len(timeline.merge(segments, tolerance_ms=1000)) == 2
    assert len(timeline.merge(segments, tolerance_ms=5000)) == 1


def test_overlapping_segments_extend_rather_than_shrink():
    segments = [seg(0, T0, 5 * MIN), seg(1, T0 + MIN, MIN)]
    [merged] = timeline.merge(segments)
    assert merged.end_ts == T0 + 5 * MIN


def test_unordered_input_is_sorted():
    segments = list(reversed(run(5)))
    [merged] = timeline.merge(segments)
    assert merged.start_ts == T0
    assert merged.segment_count == 5


def test_merging_nothing():
    assert timeline.merge([]) == []


# -------------------------------------------------------------------- overlap


def test_overlapping_includes_a_segment_straddling_the_start():
    """Its first frames are inside the window, so it must be included."""
    straddler = seg(0, T0 - 30_000, MIN)
    assert timeline.overlapping([straddler], T0, T0 + HOUR) == [straddler]


def test_overlapping_excludes_a_segment_that_ends_exactly_at_the_start():
    ends_at_start = seg(0, T0 - MIN, MIN)
    assert timeline.overlapping([ends_at_start], T0, T0 + HOUR) == []


def test_overlapping_excludes_a_segment_starting_at_the_end():
    assert timeline.overlapping([seg(0, T0 + HOUR)], T0, T0 + HOUR) == []


# ----------------------------------------------------------------------- gaps


def test_gaps_include_the_leading_and_trailing_holes():
    ranges = [Range(T0 + HOUR, T0 + 2 * HOUR)]
    holes = timeline.gaps(ranges, T0, T0 + 3 * HOUR)
    assert [(h.start_ts, h.end_ts) for h in holes] == [
        (T0, T0 + HOUR),
        (T0 + 2 * HOUR, T0 + 3 * HOUR),
    ]


def test_full_coverage_has_no_gaps():
    assert timeline.gaps([Range(T0, T0 + HOUR)], T0, T0 + HOUR) == []


def test_an_empty_day_is_one_whole_gap():
    [hole] = timeline.gaps([], T0, T0 + DAY)
    assert hole.duration_ms == DAY


def test_gaps_are_clipped_to_the_window():
    ranges = [Range(T0 - HOUR, T0 + HOUR)]
    assert timeline.gaps(ranges, T0, T0 + 2 * HOUR)[0].start_ts == T0 + HOUR


# ------------------------------------------------------------------- coverage


def test_coverage_counts_only_the_part_inside_the_window():
    ranges = [Range(T0 - HOUR, T0 + HOUR)]
    assert timeline.coverage_ms(ranges, T0, T0 + 2 * HOUR) == HOUR


def test_coverage_of_nothing_is_zero():
    assert timeline.coverage_ms([], T0, T0 + DAY) == 0


# ---------------------------------------------------------------------- build


def test_build_reports_ranges_gaps_and_coverage():
    segments = run(60) + run(60, start=T0 + 2 * HOUR)
    payload = timeline.build(segments, T0, T0 + 4 * HOUR)
    assert len(payload["ranges"]) == 2
    assert payload["segment_count"] == 120
    assert payload["covered_ms"] == 2 * HOUR
    assert payload["coverage_percent"] == 50.0
    assert payload["not_yet"] is None


def test_build_separates_the_future_from_a_gap():
    """A bar for today must not paint the evening as missing footage."""
    now = T0 + 6 * HOUR
    payload = timeline.build(run(60), T0, T0 + DAY, now_ms=now)
    assert payload["not_yet"]["start_ts"] == now
    assert payload["not_yet"]["end_ts"] == T0 + DAY
    # The hole between the footage and now is a real gap; after now is not.
    assert all(gap["end_ts"] <= now for gap in payload["gaps"])
    assert payload["coverage_percent"] > 0


def test_build_on_a_day_with_nothing_recorded():
    payload = timeline.build([], T0, T0 + DAY)
    assert payload["ranges"] == []
    assert payload["coverage_percent"] == 0.0
    assert len(payload["gaps"]) == 1


def test_build_percentage_is_measured_against_the_elapsed_part_of_today():
    now = T0 + 2 * HOUR
    payload = timeline.build(run(120), T0, T0 + DAY, now_ms=now)
    # Two hours recorded out of two hours elapsed.
    assert payload["coverage_percent"] == 100.0


# -------------------------------------------------------------- day boundaries


def test_day_bounds_is_exactly_a_day_in_utc():
    start, end = timeline.day_bounds("2026-10-27", timezone.utc)
    assert end - start == DAY


def test_day_bounds_handles_a_dst_change():
    """A local day is not always 24 hours; the end is the next midnight."""
    try:
        from zoneinfo import ZoneInfo
    except ModuleNotFoundError:
        return
    eastern = ZoneInfo("America/New_York")
    start, end = timeline.day_bounds("2026-11-01", eastern)  # US fall-back
    assert end - start == 25 * HOUR


# --------------------------------------------------------------------- nearest


def test_nearest_prefers_the_containing_segment():
    segments = run(5)
    found = timeline.nearest_segment(segments, T0 + 2 * MIN + 30_000)
    assert found.id == 2


def test_nearest_falls_back_to_the_closest_segment():
    """The pointer rarely lands inside recorded footage."""
    segments = run(2)
    found = timeline.nearest_segment(segments, T0 + 10 * MIN)
    assert found.id == 1


def test_nearest_on_nothing():
    assert timeline.nearest_segment([], T0) is None
