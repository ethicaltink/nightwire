"""End-to-end event handling, driven by the fake PullPoint responder.

open and close, merging inside 10
seconds, the 10-minute cap, and a reconnect closing anything left open, all
through the real tracker, the real store and a real SQLite file, with no
camera and no network.
"""

import asyncio

from nightwire import cameras, db
from nightwire.events import store
from nightwire.events.listener import EventSupervisor, CameraListener
from tests import fake_pullpoint as fake

SECRET = "s" * 40
T0 = 1_793_000_000_000
SECOND = 1000
MIN = 60 * SECOND


def build(tmp_path, sources=None):
    conn = db.connect(tmp_path / "nightwire.db")
    db.migrate(conn)
    cameras.create(
        conn,
        SECRET,
        camera_id="front",
        name="Front Door",
        host="192.168.1.101",
        main_uri="rtsp://x",
    )
    store.replace_sources(
        conn,
        "front",
        sources
        or [
            {
                "topic": fake.VMD_TOPIC,
                "source_filter": None,
                "label": "Porch",
                "kind": "span",
                "color": "#FF2BD6",
            },
            {
                "topic": fake.FENCE_TOPIC,
                "source_filter": None,
                "label": "Walkway line",
                "kind": "point",
                "color": "#FF8A1E",
            },
        ],
    )
    # Snapshots off: they would need go2rtc and an event loop task.
    supervisor = EventSupervisor(
        conn, tmp_path / "recordings", secret_key=SECRET, snapshots=False
    )
    listener = CameraListener("front", "Front Door")
    supervisor.listeners["front"] = listener
    return conn, supervisor, listener


def rows(conn):
    return list(conn.execute("SELECT * FROM events ORDER BY id"))


def feed(supervisor, listener, notifications):
    supervisor._handle(listener, notifications)


# ----------------------------------------------------------- open and close


def test_motion_on_then_off_makes_one_event(tmp_path):
    conn, supervisor, listener = build(tmp_path)
    feed(supervisor, listener, [fake.motion(T0, True)])
    feed(supervisor, listener, [fake.motion(T0 + 30 * SECOND, False)])

    [event] = rows(conn)
    assert abs(event["start_ts"] - T0) <= 1
    assert abs(event["end_ts"] - (T0 + 30 * SECOND)) <= 1
    assert event["camera_id"] == "front"


def test_an_event_is_open_until_it_closes(tmp_path):
    conn, supervisor, listener = build(tmp_path)
    feed(supervisor, listener, [fake.motion(T0, True)])
    [event] = rows(conn)
    assert event["end_ts"] is None
    assert store.open_now(conn)[0]["label"] == "Porch"


def test_a_batch_containing_both_transitions_is_handled_in_order(tmp_path):
    conn, supervisor, listener = build(tmp_path)
    feed(
        supervisor,
        listener,
        [fake.motion(T0, True), fake.motion(T0 + 20 * SECOND, False)],
    )
    [event] = rows(conn)
    assert event["end_ts"] is not None


def test_the_listener_counts_what_it_saw(tmp_path):
    conn, supervisor, listener = build(tmp_path)
    feed(supervisor, listener, [fake.motion(T0, True), fake.motion(T0 + MIN, False)])
    assert listener.event_count == 2
    assert listener.last_event_ms is not None


def test_a_notification_for_an_unconfigured_topic_is_ignored(tmp_path):
    conn, supervisor, listener = build(tmp_path)
    feed(supervisor, listener, [fake.notification("vendor:Something/Else", T0, data={"x": "true"})])
    assert rows(conn) == []
    assert listener.event_count == 0


# ------------------------------------------------------------------ merging


def test_flicker_inside_ten_seconds_makes_one_event(tmp_path):
    """One person walking past must not leave a row of markers."""
    conn, supervisor, listener = build(tmp_path)
    feed(supervisor, listener, [fake.motion(T0, True)])
    feed(supervisor, listener, [fake.motion(T0 + 2 * SECOND, False)])
    feed(supervisor, listener, [fake.motion(T0 + 5 * SECOND, True)])
    feed(supervisor, listener, [fake.motion(T0 + 40 * SECOND, False)])

    found = rows(conn)
    assert len(found) == 1
    assert abs(found[0]["start_ts"] - T0) <= 1
    assert abs(found[0]["end_ts"] - (T0 + 40 * SECOND)) <= 1


def test_a_gap_over_ten_seconds_makes_two_events(tmp_path):
    conn, supervisor, listener = build(tmp_path)
    feed(supervisor, listener, [fake.motion(T0, True)])
    feed(supervisor, listener, [fake.motion(T0 + 2 * SECOND, False)])
    feed(supervisor, listener, [fake.motion(T0 + 30 * SECOND, True)])
    assert len(rows(conn)) == 2


# -------------------------------------------------------------------- points


def test_a_line_crossing_makes_a_point_event(tmp_path):
    conn, supervisor, listener = build(tmp_path)
    feed(supervisor, listener, [fake.crossing(T0)])
    [event] = rows(conn)
    assert event["start_ts"] == event["end_ts"]


def test_two_crossings_make_two_events(tmp_path):
    """Point events never merge: two crossings are two facts."""
    conn, supervisor, listener = build(tmp_path)
    feed(supervisor, listener, [fake.crossing(T0)])
    feed(supervisor, listener, [fake.crossing(T0 + SECOND)])
    assert len(rows(conn)) == 2


def test_a_count_field_is_not_mistaken_for_a_state(tmp_path):
    """`Count: "1"` means one crossing, not motion switching on."""
    conn, supervisor, listener = build(tmp_path)
    feed(supervisor, listener, [fake.crossing(T0)])
    [event] = rows(conn)
    assert event["end_ts"] == event["start_ts"], "stored as a point, not left open"


# ----------------------------------------------------------------- the cap


def test_an_event_still_open_after_ten_minutes_is_split(tmp_path):
    conn, supervisor, listener = build(tmp_path)
    feed(supervisor, listener, [fake.motion(T0, True)])
    effects = listener.tracker.tick(T0 + 10 * MIN)
    supervisor._apply(listener, effects)

    found = rows(conn)
    assert len(found) == 2
    assert found[0]["end_ts"] == T0 + 10 * MIN
    assert found[1]["start_ts"] == T0 + 10 * MIN
    assert found[1]["end_ts"] is None, "still on, so the new block is open"


# ------------------------------------------------------------------ reconnect


def test_a_reconnect_closes_what_the_camera_left_open(tmp_path):
    """The Done-when: a camera reboot leaves nothing stuck open."""
    conn, supervisor, listener = build(tmp_path)
    feed(supervisor, listener, [fake.motion(T0, True)])
    supervisor._apply(listener, listener.tracker.on_reconnect(T0 + 2 * MIN))

    [event] = rows(conn)
    assert event["end_ts"] == T0 + 2 * MIN
    assert store.open_now(conn) == []


def test_after_a_reconnect_reported_state_opens_a_fresh_event(tmp_path):
    conn, supervisor, listener = build(tmp_path)
    feed(supervisor, listener, [fake.motion(T0, True)])
    supervisor._apply(listener, listener.tracker.on_reconnect(T0 + MIN))
    feed(supervisor, listener, [fake.motion(T0 + MIN + SECOND, True)])

    found = rows(conn)
    assert len(found) == 2, "not merged across the outage"
    assert found[1]["end_ts"] is None


def test_startup_closes_events_a_crash_left_open(tmp_path):
    conn, supervisor, listener = build(tmp_path)
    feed(supervisor, listener, [fake.motion(T0, True)])
    # Simulate a hard restart: new process, nothing in memory.
    assert store.close_dangling(conn, "front", T0 + 5 * MIN) == 1
    assert store.open_now(conn) == []


# ------------------------------------------------------- the clock correction


def test_event_times_are_corrected_onto_the_nvr_clock(tmp_path):
    """A camera three seconds fast must not misplace its markers."""
    from nightwire.events import clock

    conn, supervisor, listener = build(tmp_path)
    store.save_offset(conn, clock.measure("front", T0 + 3000, T0, T0))

    feed(supervisor, listener, [fake.motion(T0 + 3000, True)])
    [event] = rows(conn)
    # Stored on the NVR clock...
    assert abs(event["start_ts"] - T0) <= 1
    # ...with the camera's own reading kept for auditing.
    assert abs(event["camera_ts"] - (T0 + 3000)) <= 1


def test_an_absurd_offset_is_not_applied(tmp_path):
    from nightwire.events import clock

    conn, supervisor, listener = build(tmp_path)
    store.save_offset(conn, clock.measure("front", T0 + 400 * 86_400_000, T0, T0))
    feed(supervisor, listener, [fake.motion(T0, True)])
    [event] = rows(conn)
    assert abs(event["start_ts"] - T0) <= 1


# ------------------------------------------------------------------- health


def test_health_reports_subscription_state(tmp_path):
    conn, supervisor, listener = build(tmp_path)
    supervisor.started = True
    listener.state = "subscribed"
    ok, state, detail = supervisor.health()
    assert ok is True and state == "subscribed"
    assert "1 of 1" in detail


def test_a_camera_with_no_sources_is_not_a_fault(tmp_path):
    conn, supervisor, listener = build(tmp_path, sources=[])
    supervisor.started = True
    listener.state = "no sources"
    ok, state, _detail = supervisor.health()
    assert ok is True and state == "no sources"


def test_a_reconnecting_listener_is_degraded_not_unhealthy(tmp_path):
    """Same rule as the recorder: report it, do not restart the container."""
    conn, supervisor, listener = build(tmp_path)
    supervisor.started = True
    listener.state = "reconnecting"
    ok, state, detail = supervisor.health()
    assert ok is True
    assert state == "degraded"
    assert "front" in detail


def test_status_for_an_unknown_camera(tmp_path):
    _conn, supervisor, _listener = build(tmp_path)
    assert supervisor.status("nope")["state"] == "stopped"


# ------------------------------------------------------- the fake responder


def test_the_fake_pullpoint_behaves_like_a_subscription():
    point = fake.FakePullPoint([[fake.motion(T0, True)], []])

    async def drive():
        await point.connect(["topic"])
        first = await point.pull()
        second = await point.pull()
        await point.renew()
        try:
            await point.pull()
        except ConnectionError:
            pass
        else:
            raise AssertionError("expected the subscription to drop")
        await point.close()
        return first, second

    first, second = asyncio.run(drive())
    assert len(first) == 1 and second == []
    assert point.connected and point.closed and point.renewals == 1
