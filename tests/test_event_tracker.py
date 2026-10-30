"""The event lifecycle rules."""

from nightwire.events import tracker as tracker_mod
from nightwire.events.tracker import Close, EventTracker, Open, Point, Reopen

T0 = 1_793_000_000_000
SECOND = 1000
MINUTE = 60 * SECOND


def tracked(**kw):
    return EventTracker(**kw)


def with_ids(tracker, effects, start_id=1):
    """Pretend the store assigned row ids, as it does in production."""
    next_id = start_id
    for effect in effects:
        if isinstance(effect, Open):
            tracker.note_event_id(effect.source_id, next_id)
            next_id += 1
    return next_id


# ------------------------------------------------------------- open and close


def test_true_opens_and_false_closes():
    tracker = tracked()
    [opened] = tracker.on_value(1, True, T0)
    assert isinstance(opened, Open) and opened.start_ts == T0
    with_ids(tracker, [opened])
    assert tracker.is_open(1)

    [closed] = tracker.on_value(1, False, T0 + MINUTE)
    assert isinstance(closed, Close) and closed.end_ts == T0 + MINUTE
    assert not tracker.is_open(1)


def test_a_repeated_true_is_not_a_second_event():
    tracker = tracked()
    with_ids(tracker, tracker.on_value(1, True, T0))
    assert tracker.on_value(1, True, T0 + SECOND) == []
    assert tracker.open_since(1) == T0


def test_a_false_with_nothing_open_is_ignored():
    """Normal right after subscribing: the camera reports current state."""
    assert tracked().on_value(1, False, T0) == []


def test_sources_are_tracked_independently():
    tracker = tracked()
    with_ids(tracker, tracker.on_value(1, True, T0))
    with_ids(tracker, tracker.on_value(2, True, T0 + SECOND), start_id=2)
    assert tracker.open_sources() == [1, 2]
    tracker.on_value(1, False, T0 + MINUTE)
    assert tracker.open_sources() == [2]


def test_a_close_before_the_open_cannot_invert_the_event():
    tracker = tracked()
    with_ids(tracker, tracker.on_value(1, True, T0))
    [closed] = tracker.on_value(1, False, T0 - MINUTE)
    assert closed.end_ts == T0


# -------------------------------------------------------------------- merging


def test_events_less_than_ten_seconds_apart_merge():
    """One person walking past must not leave twenty markers."""
    tracker = tracked()
    with_ids(tracker, tracker.on_value(1, True, T0))
    tracker.on_value(1, False, T0 + 5 * SECOND)
    effects = tracker.on_value(1, True, T0 + 9 * SECOND)
    assert len(effects) == 1
    assert isinstance(effects[0], Reopen)
    assert effects[0].event_id == 1


def test_events_more_than_ten_seconds_apart_stay_separate():
    tracker = tracked()
    with_ids(tracker, tracker.on_value(1, True, T0))
    tracker.on_value(1, False, T0 + 5 * SECOND)
    [effect] = tracker.on_value(1, True, T0 + 20 * SECOND)
    assert isinstance(effect, Open)


def test_the_merge_boundary_is_inclusive():
    tracker = tracked()
    with_ids(tracker, tracker.on_value(1, True, T0))
    tracker.on_value(1, False, T0 + SECOND)
    [effect] = tracker.on_value(1, True, T0 + SECOND + 10 * SECOND)
    assert isinstance(effect, Reopen), "exactly 10s apart merges"


def test_the_merge_window_is_configurable():
    tracker = tracked(merge_window_ms=2 * SECOND)
    with_ids(tracker, tracker.on_value(1, True, T0))
    tracker.on_value(1, False, T0 + SECOND)
    [effect] = tracker.on_value(1, True, T0 + 5 * SECOND)
    assert isinstance(effect, Open)


def test_merging_only_applies_within_one_source():
    tracker = tracked()
    with_ids(tracker, tracker.on_value(1, True, T0))
    tracker.on_value(1, False, T0 + SECOND)
    [effect] = tracker.on_value(2, True, T0 + 2 * SECOND)
    assert isinstance(effect, Open)


def test_a_reopened_event_can_close_again():
    tracker = tracked()
    with_ids(tracker, tracker.on_value(1, True, T0))
    tracker.on_value(1, False, T0 + SECOND)
    tracker.on_value(1, True, T0 + 2 * SECOND)
    [closed] = tracker.on_value(1, False, T0 + MINUTE)
    assert isinstance(closed, Close) and closed.event_id == 1


# ------------------------------------------------------------------- the cap


def test_nothing_happens_before_ten_minutes():
    tracker = tracked()
    with_ids(tracker, tracker.on_value(1, True, T0))
    assert tracker.tick(T0 + 9 * MINUTE) == []


def test_an_open_event_is_capped_at_ten_minutes():
    """A stuck `true` must not paint the whole day as motion."""
    tracker = tracked()
    with_ids(tracker, tracker.on_value(1, True, T0))
    effects = tracker.tick(T0 + 10 * MINUTE)
    assert [type(e).__name__ for e in effects] == ["Close", "Open"]
    assert effects[0].end_ts == T0 + 10 * MINUTE
    assert effects[1].start_ts == T0 + 10 * MINUTE
    assert tracker.is_open(1), "still on, so a new block starts rather than a gap"


def test_the_cap_applies_repeatedly():
    tracker = tracked()
    next_id = with_ids(tracker, tracker.on_value(1, True, T0))
    first = tracker.tick(T0 + 10 * MINUTE)
    next_id = with_ids(tracker, first, start_id=next_id)
    second = tracker.tick(T0 + 20 * MINUTE)
    assert [type(e).__name__ for e in second] == ["Close", "Open"]


def test_the_cap_ignores_a_source_with_no_row_id_yet():
    tracker = tracked()
    tracker.on_value(1, True, T0)  # no note_event_id
    assert tracker.tick(T0 + 10 * MINUTE) == []
    assert tracker.is_open(1)


def test_the_cap_is_configurable():
    tracker = tracked(max_open_ms=MINUTE)
    with_ids(tracker, tracker.on_value(1, True, T0))
    assert tracker.tick(T0 + MINUTE) != []


# ------------------------------------------------------------------ reconnect


def test_a_reconnect_closes_everything_open():
    """A camera reboot must not leave an event open forever."""
    tracker = tracked()
    next_id = with_ids(tracker, tracker.on_value(1, True, T0))
    with_ids(tracker, tracker.on_value(2, True, T0 + SECOND), start_id=next_id)
    effects = tracker.on_reconnect(T0 + MINUTE)
    assert [type(e).__name__ for e in effects] == ["Close", "Close"]
    assert all(e.end_ts == T0 + MINUTE for e in effects)
    assert tracker.open_sources() == []


def test_a_reconnect_with_nothing_open_does_nothing():
    assert tracked().on_reconnect(T0) == []


def test_a_reconnect_does_not_merge_across_the_outage():
    """Merging across a gap would stitch two incidents into one block."""
    tracker = tracked()
    with_ids(tracker, tracker.on_value(1, True, T0))
    tracker.on_reconnect(T0 + SECOND)
    [effect] = tracker.on_value(1, True, T0 + 2 * SECOND)
    assert isinstance(effect, Open), "a fresh subscription starts a fresh event"


def test_resubscribing_reopens_from_reported_state():
    """A new subscription receives current state, so `on` opens an event."""
    tracker = tracked()
    tracker.on_reconnect(T0)
    [effect] = tracker.on_value(1, True, T0 + SECOND)
    assert isinstance(effect, Open)


# --------------------------------------------------------------------- points


def test_a_point_event_has_no_state():
    tracker = tracked()
    [effect] = tracker.on_point(1, T0)
    assert isinstance(effect, Point) and effect.start_ts == T0
    assert not tracker.is_open(1)


def test_points_do_not_merge():
    tracker = tracked()
    first = tracker.on_point(1, T0)
    second = tracker.on_point(1, T0 + SECOND)
    assert len(first) == len(second) == 1


def test_forget_drops_a_disabled_sources_state():
    tracker = tracked()
    with_ids(tracker, tracker.on_value(1, True, T0))
    tracker.forget(1)
    assert not tracker.is_open(1)
    assert tracker.on_value(1, False, T0 + MINUTE) == []


def test_the_defaults_match_the_spec():
    assert tracker_mod.MERGE_WINDOW_MS == 10_000
    assert tracker_mod.MAX_OPEN_MS == 600_000
