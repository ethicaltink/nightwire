"""Camera clock offset correction."""

from datetime import datetime, timezone

from nightwire.events import clock

T0 = 1_793_000_000_000


def test_a_synchronised_camera_has_no_offset():
    offset = clock.measure("front", T0, T0 - 10, T0 + 10)
    assert offset.offset_ms == 0
    assert not offset.warn
    assert offset.usable_offset_ms == 0


def test_latency_is_split_rather_than_counted_as_skew():
    # 200 ms round trip, camera agrees with the midpoint.
    offset = clock.measure("front", T0 + 100, T0, T0 + 200)
    assert offset.offset_ms == 0
    assert offset.round_trip_ms == 200


def test_a_fast_camera_gives_a_positive_offset():
    offset = clock.measure("front", T0 + 5000, T0, T0)
    assert offset.offset_ms == 5000
    assert offset.warn
    assert "ahead of" in offset.detail


def test_a_slow_camera_gives_a_negative_offset():
    offset = clock.measure("front", T0 - 5000, T0, T0)
    assert offset.offset_ms == -5000
    assert "behind" in offset.detail


def test_the_warning_threshold_is_two_seconds():
    assert not clock.measure("front", T0 + 1900, T0, T0).warn
    assert clock.measure("front", T0 + 2100, T0, T0).warn


def test_an_absurd_reading_is_not_applied():
    """A camera with no NTP sitting at its factory date must not move events."""
    offset = clock.measure("front", T0 + 400 * 86_400_000, T0, T0)
    assert not offset.plausible
    assert offset.usable_offset_ms == 0
    assert "too far to be clock drift" in offset.detail


def test_correction_moves_a_camera_time_onto_the_nvr_clock():
    offset = clock.measure("front", T0 + 3000, T0, T0)
    # The camera says 14:32:03 when the NVR says 14:32:00.
    assert clock.correct(T0 + 3000, offset) == T0


def test_correction_without_an_offset_is_the_identity():
    assert clock.correct(T0, None) == T0


def test_correction_with_an_absurd_offset_is_the_identity():
    offset = clock.measure("front", T0 + 400 * 86_400_000, T0, T0)
    assert clock.correct(T0, offset) == T0


def test_offset_serialises_for_core():
    data = clock.measure("front", T0 + 3000, T0, T0).as_dict()
    assert data["camera_id"] == "front"
    assert data["offset_ms"] == 3000
    assert data["applied_ms"] == 3000
    assert data["warn"] is True
    assert data["detail"]


def test_parse_utc_handles_iso_strings():
    assert clock.parse_utc("2026-10-27T14:32:05Z") == int(
        datetime(2026, 10, 27, 14, 32, 5, tzinfo=timezone.utc).timestamp() * 1000
    )
    assert clock.parse_utc("2026-10-27T14:32:05+00:00") == clock.parse_utc("2026-10-27T14:32:05Z")


def test_parse_utc_assumes_utc_for_a_naive_string():
    assert clock.parse_utc("2026-10-27T14:32:05") == clock.parse_utc("2026-10-27T14:32:05Z")


def test_parse_utc_handles_the_structured_form():
    structured = {
        "Date": {"Year": 2026, "Month": 10, "Day": 27},
        "Time": {"Hour": 14, "Minute": 32, "Second": 5},
    }
    assert clock.parse_utc(structured) == clock.parse_utc("2026-10-27T14:32:05Z")


def test_parse_utc_handles_datetimes_and_numbers():
    moment = datetime(2026, 10, 27, 14, 32, 5, tzinfo=timezone.utc)
    assert clock.parse_utc(moment) == int(moment.timestamp() * 1000)
    assert clock.parse_utc(moment.timestamp()) == int(moment.timestamp() * 1000)
    assert clock.parse_utc(int(moment.timestamp() * 1000)) == int(moment.timestamp() * 1000)


def test_parse_utc_rejects_junk():
    for junk in [None, "", "not a time", {}, {"Date": {}}, []]:
        assert clock.parse_utc(junk) is None
