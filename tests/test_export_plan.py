"""Export planning: which segments, what offset, how long, what name."""

from datetime import timezone

from nightwire.export import plan
from nightwire.playback.timeline import Segment

MIN = 60_000
HOUR = 60 * MIN
T0 = 1_793_000_000_000


def seg(index, start, duration=MIN):
    return Segment(
        id=index,
        start_ts=start,
        end_ts=start + duration,
        duration_ms=duration,
        path=f"front/2026/10/27/14/{index}.ts",
    )


def run(count, start=T0, duration=MIN):
    return [seg(i, start + i * duration, duration) for i in range(count)]


# ------------------------------------------------------------------ validation


def test_a_backwards_range_is_refused():
    try:
        plan.build("front", run(5), T0 + MIN, T0)
    except plan.ExportError as exc:
        assert "end must be after" in str(exc)
    else:
        raise AssertionError("expected ExportError")


def test_a_sub_second_clip_is_refused():
    try:
        plan.build("front", run(5), T0, T0 + 500)
    except plan.ExportError as exc:
        assert "one second" in str(exc)
    else:
        raise AssertionError("expected ExportError")


def test_the_two_hour_limit_is_enforced():
    segments = run(200, duration=MIN)
    try:
        plan.build("front", segments, T0, T0 + 3 * HOUR)
    except plan.ExportError as exc:
        assert "limited to 2 hours" in str(exc)
        assert "shorter range" in str(exc)
    else:
        raise AssertionError("expected ExportError")


def test_exactly_two_hours_is_allowed():
    segments = run(130, duration=MIN)
    built = plan.build("front", segments, T0, T0 + 2 * HOUR)
    assert built.duration_ms == 2 * HOUR


def test_the_limit_is_configurable():
    built = plan.build("front", run(10), T0, T0 + 5 * MIN, max_duration_ms=10 * MIN)
    assert built.duration_ms == 5 * MIN


def test_a_range_with_no_footage_is_refused():
    try:
        plan.build("front", run(5), T0 + 10 * HOUR, T0 + 10 * HOUR + MIN)
    except plan.ExportError as exc:
        assert "no recorded footage" in str(exc)
    else:
        raise AssertionError("expected ExportError")


# --------------------------------------------------------------------- the cut


def test_only_the_covering_segments_are_included():
    built = plan.build("front", run(10), T0 + 2 * MIN, T0 + 4 * MIN)
    assert [s.id for s in built.segments] == [2, 3]


def test_a_segment_straddling_the_start_is_included():
    """Its tail is the first footage the person asked for."""
    built = plan.build("front", run(10), T0 + 2 * MIN + 30_000, T0 + 4 * MIN)
    assert built.segments[0].id == 2


def test_the_seek_offset_is_measured_from_the_first_segment():
    built = plan.build("front", run(10), T0 + 2 * MIN + 30_000, T0 + 4 * MIN)
    # The concat timeline starts at segment 2, so 30 s into it.
    assert built.seek_offset_ms == 30_000


def test_the_seek_offset_is_zero_on_an_exact_boundary():
    assert plan.build("front", run(10), T0 + 2 * MIN, T0 + 3 * MIN).seek_offset_ms == 0


def test_the_seek_offset_is_never_negative():
    """A clip starting before the first segment must not seek backwards."""
    built = plan.build("front", run(3, start=T0 + HOUR), T0 + HOUR - MIN, T0 + HOUR + MIN)
    assert built.seek_offset_ms == 0


def test_the_duration_is_what_was_asked_for():
    built = plan.build("front", run(10), T0 + MIN, T0 + 5 * MIN)
    assert built.duration_ms == 4 * MIN


# ------------------------------------------------------------------- coverage


def test_full_coverage_reports_no_gaps():
    built = plan.build("front", run(10), T0 + MIN, T0 + 5 * MIN)
    assert built.covered_ms == 4 * MIN
    assert built.has_gaps is False


def test_a_clip_spanning_a_gap_is_allowed_but_flagged():
    """Exporting over the cable-pull gap is legitimate; it is just reported."""
    segments = run(2) + run(2, start=T0 + 10 * MIN)
    built = plan.build("front", segments, T0, T0 + 12 * MIN)
    assert built.has_gaps is True
    assert built.covered_ms < built.duration_ms


def test_a_clip_entirely_inside_one_segment():
    built = plan.build("front", run(5), T0 + 10_000, T0 + 20_000)
    assert len(built.segments) == 1
    assert built.seek_offset_ms == 10_000
    assert built.has_gaps is False


# ----------------------------------------------------------------- the filename


def test_the_filename_follows_the_spec():
    name = plan.export_name("front", T0, 10 * MIN, timezone.utc)
    assert name.startswith("front_")
    assert name.endswith("_10m00s.mp4")
    assert len(name.split("_")) == 3


def test_filename_durations_read_naturally():
    assert plan.export_name("front", T0, 45_000, timezone.utc).endswith("_45s.mp4")
    assert plan.export_name("front", T0, 90_000, timezone.utc).endswith("_1m30s.mp4")
    assert plan.export_name("front", T0, HOUR, timezone.utc).endswith("_1h00m.mp4")
    assert plan.export_name("front", T0, 2 * HOUR, timezone.utc).endswith("_2h00m.mp4")


def test_the_filename_timestamp_is_local():
    try:
        from zoneinfo import ZoneInfo
    except ModuleNotFoundError:
        return
    utc = plan.export_name("front", T0, MIN, timezone.utc)
    eastern = plan.export_name("front", T0, MIN, ZoneInfo("America/New_York"))
    assert utc != eastern, "the stamp is the local wall clock, not UTC"


def test_the_plan_serialises_for_the_api():
    data = plan.build("front", run(5), T0, T0 + 2 * MIN, precise=True).as_dict()
    assert data["camera_id"] == "front"
    assert data["precise"] is True
    assert data["segment_count"] == 2
    assert data["name"].endswith(".mp4")


# --------------------------------------------------------------- the concat list


def test_the_concat_list_is_absolute_and_in_order(tmp_path):
    built = plan.build("front", run(3), T0, T0 + 3 * MIN)
    text = plan.concat_list(built, tmp_path)
    lines = text.strip().splitlines()
    assert len(lines) == 3
    assert all(line.startswith(f"file '{tmp_path}") for line in lines)
    assert lines[0].endswith("/0.ts'")
    assert lines[2].endswith("/2.ts'")


def test_the_concat_list_escapes_a_quote_in_a_path(tmp_path):
    """The path is data; a quote in it must not break the list."""
    odd = Segment(id=1, start_ts=T0, end_ts=T0 + MIN, duration_ms=MIN, path="we'rd/x.ts")
    built = plan.build("front", [odd], T0, T0 + MIN)
    text = plan.concat_list(built, tmp_path)
    assert r"'\''" in text


def test_the_concat_list_ends_with_a_newline(tmp_path):
    built = plan.build("front", run(1), T0, T0 + MIN)
    assert plan.concat_list(built, tmp_path).endswith("\n")
