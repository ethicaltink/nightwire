"""HLS VOD playlist generation, with gaps. The spec asks for this by name."""

from nightwire.playback import playlist
from nightwire.playback.timeline import Segment

MIN = 60_000
HOUR = 60 * MIN
T0 = 1_793_000_000_000


def seg(index, start, duration=MIN, path=None):
    return Segment(
        id=index,
        start_ts=start,
        end_ts=start + duration,
        duration_ms=duration,
        path=path or f"front/2026/10/27/14/{index}.ts",
    )


def run(count, start=T0, duration=MIN, step=None):
    step = duration if step is None else step
    return [seg(i, start + i * step, duration) for i in range(count)]


def lines(text):
    return [line for line in text.splitlines() if line.strip()]


# ------------------------------------------------------------------- structure


def test_a_playlist_has_the_required_vod_headers():
    text = playlist.build(run(3), T0, T0 + HOUR)
    head = lines(text)[:5]
    assert head[0] == "#EXTM3U"
    assert head[1] == "#EXT-X-VERSION:3"
    assert head[2] == "#EXT-X-PLAYLIST-TYPE:VOD"
    assert head[3].startswith("#EXT-X-TARGETDURATION:")
    assert head[4] == "#EXT-X-MEDIA-SEQUENCE:0"


def test_a_vod_playlist_must_end_with_endlist():
    """Without it players treat the playlist as live and will not seek."""
    text = playlist.build(run(3), T0, T0 + HOUR)
    assert lines(text)[-1] == "#EXT-X-ENDLIST"


def test_every_segment_carries_a_program_date_time_and_extinf():
    text = playlist.build(run(3), T0, T0 + HOUR)
    assert text.count("#EXT-X-PROGRAM-DATE-TIME:") == 3
    assert text.count("#EXTINF:") == 3


def test_segment_uris_point_at_the_media_route():
    text = playlist.build(run(1), T0, T0 + HOUR)
    assert "/media/front/2026/10/27/14/0.ts" in text


def test_the_uri_prefix_is_configurable():
    text = playlist.build(run(1), T0, T0 + HOUR, prefix="/other")
    assert "/other/front/2026/10/27/14/0.ts" in text


# -------------------------------------------------------------------- duration


def test_extinf_uses_the_measured_duration_not_the_nominal_one():
    """A guessed duration desynchronises the playhead silently."""
    text = playlist.build([seg(0, T0, 60_024)], T0, T0 + HOUR)
    assert "#EXTINF:60.024," in text


def test_target_duration_is_the_longest_segment_rounded_up():
    segments = [seg(0, T0, 60_024), seg(1, T0 + MIN, 59_000)]
    assert playlist.target_duration(segments) == 61
    assert "#EXT-X-TARGETDURATION:61" in playlist.build(segments, T0, T0 + HOUR)


def test_target_duration_is_never_zero():
    """A strict player rejects a playlist with TARGETDURATION:0."""
    assert playlist.target_duration([]) == 1
    assert playlist.target_duration([seg(0, T0, 0)]) == 1


def test_a_zero_duration_segment_is_given_its_end_ts_width():
    broken = Segment(id=0, start_ts=T0, end_ts=T0 + MIN, duration_ms=0, path="front/x.ts")
    assert "#EXTINF:60.000," in playlist.build([broken], T0, T0 + HOUR)


# ------------------------------------------------------------------------ gaps


def test_contiguous_segments_have_no_discontinuity():
    text = playlist.build(run(10), T0, T0 + HOUR)
    assert playlist.discontinuity_count(text) == 0


def test_a_gap_inserts_exactly_one_discontinuity():
    segments = run(3) + run(3, start=T0 + 10 * MIN)
    text = playlist.build(segments, T0, T0 + HOUR)
    assert playlist.discontinuity_count(text) == 1


def test_two_gaps_insert_two_discontinuities():
    segments = run(2) + run(2, start=T0 + 10 * MIN) + run(2, start=T0 + 30 * MIN)
    assert playlist.discontinuity_count(playlist.build(segments, T0, T0 + HOUR)) == 2


def test_the_discontinuity_precedes_the_segment_after_the_gap():
    segments = run(2) + run(1, start=T0 + 10 * MIN)
    found = lines(playlist.build(segments, T0, T0 + HOUR))
    marker = found.index("#EXT-X-DISCONTINUITY")
    # The next program-date-time after the marker is the post-gap segment.
    assert found[marker + 1].startswith("#EXT-X-PROGRAM-DATE-TIME:")
    assert found[marker + 3].endswith("/0.ts")


def test_sub_second_jitter_does_not_insert_a_discontinuity():
    segments = [seg(0, T0, 60_024), seg(1, T0 + 60_500, 59_900)]
    assert playlist.discontinuity_count(playlist.build(segments, T0, T0 + HOUR)) == 0


def test_a_playlist_never_starts_with_a_discontinuity():
    segments = run(2, start=T0 + 30 * MIN)
    assert lines(playlist.build(segments, T0, T0 + HOUR))[5] != "#EXT-X-DISCONTINUITY"


# ---------------------------------------------------------------------- window


def test_only_segments_overlapping_the_window_appear():
    segments = run(5) + run(5, start=T0 + 2 * HOUR)
    text = playlist.build(segments, T0, T0 + HOUR)
    assert text.count("#EXTINF:") == 5


def test_a_segment_straddling_the_window_start_is_included():
    segments = [seg(0, T0 - 30_000, MIN), seg(1, T0 + 30_000, MIN)]
    assert playlist.build(segments, T0, T0 + HOUR).count("#EXTINF:") == 2


def test_segments_appear_in_chronological_order():
    segments = list(reversed(run(5)))
    uris = [line for line in lines(playlist.build(segments, T0, T0 + HOUR)) if line.endswith(".ts")]
    assert uris == sorted(uris, key=lambda u: int(u.rsplit("/", 1)[1].split(".")[0]))


def test_an_empty_window_still_produces_a_valid_playlist():
    """The player must report "nothing here", not fail to parse."""
    text = playlist.build([], T0, T0 + HOUR)
    found = lines(text)
    assert found[0] == "#EXTM3U"
    assert found[-1] == "#EXT-X-ENDLIST"
    assert "#EXTINF:" not in text


# --------------------------------------------------------- program date time


def test_program_date_time_is_iso_utc_with_milliseconds():
    # Derived rather than hand-typed: a literal here is just a second chance
    # to get the arithmetic wrong.
    from datetime import datetime, timezone
    import re

    at_ms = 1_793_000_000_123
    rendered = playlist.program_date_time(at_ms)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", rendered)
    expected = datetime.fromtimestamp(at_ms / 1000, timezone.utc)
    assert rendered.startswith(expected.strftime("%Y-%m-%dT%H:%M:%S"))
    assert rendered.endswith(".123Z")


def test_program_date_time_rounds_down_to_the_millisecond():
    assert playlist.program_date_time(1_793_000_000_000).endswith(".000Z")


def test_the_first_program_date_time_matches_the_first_segment():
    text = playlist.build(run(3), T0, T0 + HOUR)
    first = next(line for line in lines(text) if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"))
    assert first.split(":", 1)[1] == playlist.program_date_time(T0)


def test_segment_uri_normalises_slashes():
    assert playlist.segment_uri("front/x.ts") == "/media/front/x.ts"
    assert playlist.segment_uri("/front/x.ts") == "/media/front/x.ts"
    assert playlist.segment_uri("front/x.ts", prefix="/media/") == "/media/front/x.ts"
