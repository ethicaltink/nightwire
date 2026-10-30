"""Event sources and events in SQLite."""

from nightwire import cameras, db
from nightwire.events import store
from nightwire.events import tracker as tracker_mod

SECRET = "s" * 40
T0 = 1_793_000_000_000
MIN = 60_000
DAY = 86_400_000


def fresh(tmp_path, camera_ids=("front",)):
    conn = db.connect(tmp_path / "nightwire.db")
    db.migrate(conn)
    for camera_id in camera_ids:
        cameras.create(
            conn,
            SECRET,
            camera_id=camera_id,
            name=camera_id.title(),
            host="192.168.1.101",
            main_uri="rtsp://x",
        )
    return conn


def source(topic="t/a", label="Porch", kind="span", source_filter=None, color=None):
    return {
        "topic": topic,
        "label": label,
        "kind": kind,
        "source_filter": source_filter,
        "color": color,
    }


# ---------------------------------------------------------------- the sources


def test_saving_sources(tmp_path):
    conn = fresh(tmp_path)
    saved = store.replace_sources(conn, "front", [source(), source("t/b", "Street edge")])
    assert [s["label"] for s in saved] == ["Porch", "Street edge"]
    assert all(s["enabled"] for s in saved)


def test_colours_are_handed_out_without_repeating(tmp_path):
    conn = fresh(tmp_path)
    saved = store.replace_sources(
        conn, "front", [source("t/a", "A"), source("t/b", "B"), source("t/c", "C")]
    )
    colours = [s["color"] for s in saved]
    assert len(set(colours)) == 3


def test_an_explicit_colour_is_kept(tmp_path):
    conn = fresh(tmp_path)
    [saved] = store.replace_sources(conn, "front", [source(color="#39FF88")])
    assert saved["color"] == "#39FF88"


def test_saving_again_updates_rather_than_duplicates(tmp_path):
    conn = fresh(tmp_path)
    store.replace_sources(conn, "front", [source()])
    saved = store.replace_sources(conn, "front", [source(label="Renamed")])
    assert len(saved) == 1
    assert saved[0]["label"] == "Renamed"


def test_an_unticked_source_with_no_events_is_removed(tmp_path):
    conn = fresh(tmp_path)
    store.replace_sources(conn, "front", [source("t/a", "A"), source("t/b", "B")])
    saved = store.replace_sources(conn, "front", [source("t/a", "A")])
    assert [s["topic"] for s in saved] == ["t/a"]


def test_an_unticked_source_with_events_is_disabled_not_deleted(tmp_path):
    """Deleting it would orphan a day's markers on the timeline."""
    conn = fresh(tmp_path)
    [kept] = store.replace_sources(conn, "front", [source()])
    conn.execute(
        "INSERT INTO events (camera_id, source_id, start_ts) VALUES ('front', ?, ?)",
        (kept["id"], T0),
    )
    conn.commit()

    saved = store.replace_sources(conn, "front", [])
    assert len(saved) == 1
    assert saved[0]["enabled"] == 0
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_a_disabled_source_is_not_subscribed_to(tmp_path):
    conn = fresh(tmp_path)
    [row] = store.replace_sources(conn, "front", [source()])
    conn.execute("UPDATE event_sources SET enabled = 0 WHERE id = ?", (row["id"],))
    conn.commit()
    assert store.enabled_sources(conn, "front") == []
    assert store.source_index(conn, "front") == {}


def test_source_index_is_keyed_for_matching(tmp_path):
    conn = fresh(tmp_path)
    store.replace_sources(conn, "front", [source(source_filter="ProfileID=2")])
    assert "t/a#ProfileID=2" in store.source_index(conn, "front")


def test_a_source_without_a_label_is_rejected(tmp_path):
    conn = fresh(tmp_path)
    for bad in [source(label=""), source(label="   ")]:
        try:
            store.replace_sources(conn, "front", [bad])
        except store.EventError as exc:
            assert "label" in str(exc)
            continue
        raise AssertionError("expected EventError")


def test_a_source_with_a_bad_kind_is_rejected(tmp_path):
    conn = fresh(tmp_path)
    try:
        store.replace_sources(conn, "front", [source(kind="blob")])
    except store.EventError as exc:
        assert "kind" in str(exc)
    else:
        raise AssertionError("expected EventError")


def test_sources_are_per_camera(tmp_path):
    conn = fresh(tmp_path, ("front", "yard"))
    store.replace_sources(conn, "front", [source()])
    assert store.list_sources(conn, "yard") == []


# ---------------------------------------------------------------- the effects


def applied(conn, camera_id, effects, tracker=None, camera_ts=None):
    tracker = tracker or tracker_mod.EventTracker()
    return store.apply_effects(conn, camera_id, effects, tracker, camera_ts_by_source=camera_ts)


def test_an_open_effect_inserts_an_open_row(tmp_path):
    conn = fresh(tmp_path)
    [src] = store.replace_sources(conn, "front", [source()])
    tracker = tracker_mod.EventTracker()
    changed = applied(conn, "front", [tracker_mod.Open(src["id"], T0)], tracker)

    assert changed[0]["action"] == "open"
    row = conn.execute("SELECT * FROM events").fetchone()
    assert row["start_ts"] == T0
    assert row["end_ts"] is None


def test_the_tracker_learns_the_row_id(tmp_path):
    conn = fresh(tmp_path)
    [src] = store.replace_sources(conn, "front", [source()])
    tracker = tracker_mod.EventTracker()
    tracker.on_value(src["id"], True, T0)
    applied(conn, "front", [tracker_mod.Open(src["id"], T0)], tracker)
    # It can now close the event, which it could not before.
    [closed] = tracker.on_value(src["id"], False, T0 + MIN)
    assert isinstance(closed, tracker_mod.Close)


def test_a_close_effect_sets_the_end(tmp_path):
    conn = fresh(tmp_path)
    [src] = store.replace_sources(conn, "front", [source()])
    tracker = tracker_mod.EventTracker()
    applied(conn, "front", [tracker_mod.Open(src["id"], T0)], tracker)
    event_id = conn.execute("SELECT id FROM events").fetchone()["id"]
    applied(conn, "front", [tracker_mod.Close(event_id, src["id"], T0 + MIN)], tracker)
    assert conn.execute("SELECT end_ts FROM events").fetchone()["end_ts"] == T0 + MIN


def test_a_reopen_effect_clears_the_end(tmp_path):
    conn = fresh(tmp_path)
    [src] = store.replace_sources(conn, "front", [source()])
    tracker = tracker_mod.EventTracker()
    applied(conn, "front", [tracker_mod.Open(src["id"], T0)], tracker)
    event_id = conn.execute("SELECT id FROM events").fetchone()["id"]
    applied(conn, "front", [tracker_mod.Close(event_id, src["id"], T0 + MIN)], tracker)
    applied(conn, "front", [tracker_mod.Reopen(event_id, src["id"])], tracker)
    assert conn.execute("SELECT end_ts FROM events").fetchone()["end_ts"] is None
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_a_point_effect_is_stored_closed(tmp_path):
    conn = fresh(tmp_path)
    [src] = store.replace_sources(conn, "front", [source(kind="point")])
    applied(conn, "front", [tracker_mod.Point(src["id"], T0)])
    row = conn.execute("SELECT * FROM events").fetchone()
    assert row["start_ts"] == row["end_ts"] == T0


def test_the_raw_camera_time_is_kept_alongside_the_corrected_one(tmp_path):
    """The corrected time drives the timeline; the raw one is for auditing."""
    conn = fresh(tmp_path)
    [src] = store.replace_sources(conn, "front", [source()])
    applied(
        conn,
        "front",
        [tracker_mod.Open(src["id"], T0)],
        camera_ts={src["id"]: T0 + 3000},
    )
    row = conn.execute("SELECT start_ts, camera_ts FROM events").fetchone()
    assert row["start_ts"] == T0
    assert row["camera_ts"] == T0 + 3000


# -------------------------------------------------------------- close dangling


def test_events_left_open_by_a_crash_are_closed_at_startup(tmp_path):
    """Otherwise a block grows across the timeline forever."""
    conn = fresh(tmp_path)
    [src] = store.replace_sources(conn, "front", [source()])
    conn.execute(
        "INSERT INTO events (camera_id, source_id, start_ts) VALUES ('front', ?, ?)",
        (src["id"], T0),
    )
    conn.commit()
    assert store.close_dangling(conn, "front", T0 + MIN) == 1
    assert conn.execute("SELECT end_ts FROM events").fetchone()["end_ts"] == T0 + MIN


def test_closing_dangling_events_is_a_no_op_when_there_are_none(tmp_path):
    conn = fresh(tmp_path)
    assert store.close_dangling(conn, "front", T0) == 0


def test_closing_dangling_never_moves_an_end_before_its_start(tmp_path):
    conn = fresh(tmp_path)
    [src] = store.replace_sources(conn, "front", [source()])
    conn.execute(
        "INSERT INTO events (camera_id, source_id, start_ts) VALUES ('front', ?, ?)",
        (src["id"], T0),
    )
    conn.commit()
    store.close_dangling(conn, "front", T0 - MIN)
    assert conn.execute("SELECT end_ts FROM events").fetchone()["end_ts"] == T0


# -------------------------------------------------------------------- reading


def seed_events(conn, camera_id, src_id, specs):
    """specs: [(start, end or None)]"""
    for start, end in specs:
        conn.execute(
            "INSERT INTO events (camera_id, source_id, start_ts, end_ts) VALUES (?,?,?,?)",
            (camera_id, src_id, start, end),
        )
    conn.commit()


def test_a_days_events_come_back_with_their_source_details(tmp_path):
    conn = fresh(tmp_path)
    [src] = store.replace_sources(conn, "front", [source(label="Porch", color="#FF2BD6")])
    seed_events(conn, "front", src["id"], [(T0, T0 + MIN)])
    [event] = store.events_for_day(conn, "front", T0 - MIN, T0 + DAY)
    assert event["label"] == "Porch"
    assert event["color"] == "#FF2BD6"
    assert event["duration_ms"] == MIN
    assert event["open"] is False


def test_an_open_event_appears_on_todays_timeline(tmp_path):
    conn = fresh(tmp_path)
    [src] = store.replace_sources(conn, "front", [source()])
    seed_events(conn, "front", src["id"], [(T0, None)])
    [event] = store.events_for_day(conn, "front", T0 - MIN, T0 + DAY)
    assert event["open"] is True
    assert event["duration_ms"] is None


def test_events_outside_the_window_are_excluded(tmp_path):
    conn = fresh(tmp_path)
    [src] = store.replace_sources(conn, "front", [source()])
    seed_events(conn, "front", src["id"], [(T0 - DAY, T0 - DAY + MIN), (T0, T0 + MIN)])
    assert len(store.events_for_day(conn, "front", T0 - MIN, T0 + DAY)) == 1


def test_an_event_straddling_the_window_start_is_included(tmp_path):
    conn = fresh(tmp_path)
    [src] = store.replace_sources(conn, "front", [source()])
    seed_events(conn, "front", src["id"], [(T0 - MIN, T0 + MIN)])
    assert len(store.events_for_day(conn, "front", T0, T0 + DAY)) == 1


def test_events_can_be_filtered_to_chosen_sources(tmp_path):
    conn = fresh(tmp_path)
    saved = store.replace_sources(conn, "front", [source("t/a", "A"), source("t/b", "B")])
    seed_events(conn, "front", saved[0]["id"], [(T0, T0 + MIN)])
    seed_events(conn, "front", saved[1]["id"], [(T0 + MIN, T0 + 2 * MIN)])
    filtered = store.events_for_day(
        conn, "front", T0 - MIN, T0 + DAY, source_ids=[saved[1]["id"]]
    )
    assert [e["label"] for e in filtered] == ["B"]


def test_recent_events_are_newest_first_across_cameras(tmp_path):
    conn = fresh(tmp_path, ("front", "yard"))
    [a] = store.replace_sources(conn, "front", [source("t/a", "Porch")])
    [b] = store.replace_sources(conn, "yard", [source("t/b", "Gate")])
    seed_events(conn, "front", a["id"], [(T0, T0 + MIN)])
    seed_events(conn, "yard", b["id"], [(T0 + 5 * MIN, T0 + 6 * MIN)])
    found = store.recent(conn, 10)
    assert [e["label"] for e in found] == ["Gate", "Porch"]
    assert found[0]["camera_name"] == "Yard"


def test_recent_respects_its_limit(tmp_path):
    conn = fresh(tmp_path)
    [src] = store.replace_sources(conn, "front", [source()])
    seed_events(conn, "front", src["id"], [(T0 + i * MIN, T0 + i * MIN + 1) for i in range(10)])
    assert len(store.recent(conn, 3)) == 3


def test_open_now_lists_only_open_events(tmp_path):
    conn = fresh(tmp_path)
    [src] = store.replace_sources(conn, "front", [source()])
    seed_events(conn, "front", src["id"], [(T0, T0 + MIN), (T0 + 2 * MIN, None)])
    [event] = store.open_now(conn)
    assert event["start_ts"] == T0 + 2 * MIN


def test_get_event_returns_none_for_an_unknown_id(tmp_path):
    assert store.get_event(fresh(tmp_path), 999) is None


# -------------------------------------------------------------------- offsets


def test_an_offset_round_trips(tmp_path):
    from nightwire.events import clock

    conn = fresh(tmp_path)
    offset = clock.measure("front", T0 + 3000, T0, T0)
    store.save_offset(conn, offset)
    loaded = store.load_offset(conn, "front")
    assert loaded.offset_ms == 3000
    assert store.corrected_time(conn, "front", T0 + 3000) == T0


def test_no_offset_means_no_correction(tmp_path):
    conn = fresh(tmp_path)
    assert store.load_offset(conn, "front") is None
    assert store.corrected_time(conn, "front", T0) == T0


# ------------- a colour is written into a style= attribute, so validate it ---


def test_a_colour_that_is_not_hex_is_rejected(tmp_path):
    """The value is reflected into `style=` in four Svelte components."""
    conn = fresh(tmp_path)
    for bad in [
        "red",
        "#39FF8",
        "#39FF888",
        "39FF88",
        "#39FF88; background: url(http://x)",
        "}body{display:none}",
        "#39FF88'",
    ]:
        try:
            store.replace_sources(conn, "front", [source(color=bad)])
        except store.EventError:
            continue
        raise AssertionError(f"expected {bad!r} to be rejected")


def test_a_valid_colour_is_accepted_in_either_case(tmp_path):
    conn = fresh(tmp_path)
    for good in ["#39FF88", "#39ff88", "#000000", "#ffffff"]:
        [saved] = store.replace_sources(conn, "front", [source(color=good)])
        assert saved["color"] == good
