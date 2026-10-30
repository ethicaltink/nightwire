"""Prometheus metrics: rendering and collection."""

from pathlib import Path

from nightwire import cameras, db, metrics
from nightwire.metrics import Family, Sample

SECRET = "s" * 40
MIN = 60_000
T0 = 1_793_000_000_000
GB = 1000**3


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
    (tmp_path / "recordings").mkdir(exist_ok=True)
    return conn


# ------------------------------------------------------------------ rendering


def test_rendering_a_plain_gauge():
    text = metrics.render([Family("nightwire_up", "Up.").add(1)])
    assert "# HELP nightwire_up Up." in text
    assert "# TYPE nightwire_up gauge" in text
    assert "nightwire_up 1" in text
    assert text.endswith("\n")


def test_rendering_labels_sorted_for_stability():
    family = Family("m", "H.").add(1, zebra="z", alpha="a")
    assert 'm{alpha="a",zebra="z"} 1' in metrics.render([family])


def test_a_family_with_no_samples_is_omitted():
    """Emitting a HELP with no series confuses scrapers."""
    assert metrics.render([Family("empty", "H.")]) == "\n"


def test_counter_type_is_declared():
    assert "# TYPE c counter" in metrics.render([Family("c", "H.", metrics.COUNTER).add(3)])


def test_label_values_are_escaped():
    family = Family("m", "H.").add(1, camera='we"ird\\path')
    rendered = metrics.render([family])
    assert r'\"' in rendered
    assert r"\\" in rendered


def test_integers_render_without_a_decimal_point():
    assert "m 42\n" in metrics.render([Family("m", "H.").add(42)])
    assert "m 0\n" in metrics.render([Family("m", "H.").add(0)])


def test_floats_render_without_trailing_zeros():
    assert "m 3.5\n" in metrics.render([Family("m", "H.").add(3.5)])
    assert "m 0.001\n" in metrics.render([Family("m", "H.").add(0.001)])


# ----------------------------------------------------------------- collection


def test_collecting_from_a_fresh_install(tmp_path):
    conn = fresh(tmp_path)
    families = metrics.collect(conn, tmp_path / "recordings", now_ms=T0)
    names = metrics.metric_names(families)

    assert "nightwire_up" in names
    assert "nightwire_cameras_total" in names
    assert "nightwire_disk_used_percent" in names
    rendered = metrics.render(families)
    assert "nightwire_up 1" in rendered


def test_a_camera_with_no_recorder_reports_zero_not_nothing(tmp_path):
    """An alert on "not recording" must fire, not go blind."""
    conn = fresh(tmp_path)
    rendered = metrics.render(metrics.collect(conn, tmp_path / "recordings", now_ms=T0))
    assert 'nightwire_recorder_recording{camera="front"} 0' in rendered


def test_every_recorder_state_gets_a_series(tmp_path):
    conn = fresh(tmp_path)
    rendered = metrics.render(metrics.collect(conn, tmp_path / "recordings", now_ms=T0))
    for state in metrics.RECORDER_STATES:
        assert f'state="{state}"' in rendered


def test_a_running_recorder_reports_one(tmp_path):
    class FakeRecorder:
        def status(self, camera_id):
            return {
                "state": "recording",
                "seconds_since_segment": 12.0,
                "restarts": 2,
                "watchdog_kills": 1,
            }

    conn = fresh(tmp_path)
    rendered = metrics.render(
        metrics.collect(conn, tmp_path / "recordings", now_ms=T0, recorder=FakeRecorder())
    )
    assert 'nightwire_recorder_recording{camera="front"} 1' in rendered
    assert 'nightwire_recorder_seconds_since_segment{camera="front"} 12' in rendered
    assert 'nightwire_recorder_restarts_total{camera="front"} 2' in rendered
    assert 'nightwire_recorder_watchdog_kills_total{camera="front"} 1' in rendered


def test_disk_thresholds_are_exported_so_rules_need_no_literals(tmp_path):
    conn = fresh(tmp_path)
    rendered = metrics.render(metrics.collect(conn, tmp_path / "recordings", now_ms=T0))
    assert "nightwire_disk_max_percent 90" in rendered
    assert "nightwire_disk_target_percent 85" in rendered


def test_configured_retention_is_exported_per_camera(tmp_path):
    conn = fresh(tmp_path)
    cameras.update(conn, SECRET, "front", {"retention_days": 30})
    rendered = metrics.render(metrics.collect(conn, tmp_path / "recordings", now_ms=T0))
    assert 'nightwire_camera_retention_days{camera="front"} 30' in rendered


def test_write_rate_and_projection_appear_once_there_is_footage(tmp_path):
    conn = fresh(tmp_path)
    for index in range(4):
        start = T0 - (3 - index) * 86_400_000
        conn.execute(
            "INSERT INTO segments (camera_id, start_ts, end_ts, duration_ms, path, size_bytes)"
            " VALUES ('front', ?, ?, ?, ?, ?)",
            (start, start + MIN, MIN, f"front/{index}.ts", 10 * GB),
        )
    conn.commit()
    rendered = metrics.render(metrics.collect(conn, tmp_path / "recordings", now_ms=T0))
    assert 'nightwire_camera_bytes_per_day{camera="front"}' in rendered
    assert 'nightwire_camera_mbps{camera="front"}' in rendered
    assert 'nightwire_projected_retention_days{camera="front"}' in rendered


def test_the_clock_offset_is_exported_in_seconds(tmp_path):
    from nightwire.events import clock, store

    conn = fresh(tmp_path)
    store.save_offset(conn, clock.measure("front", T0 + 3000, T0, T0))
    rendered = metrics.render(metrics.collect(conn, tmp_path / "recordings", now_ms=T0))
    assert 'nightwire_camera_clock_offset_seconds{camera="front"} 3' in rendered


def test_event_listener_state_is_exported(tmp_path):
    class FakeEvents:
        def status(self, camera_id):
            return {"state": "subscribed"}

    conn = fresh(tmp_path)
    rendered = metrics.render(
        metrics.collect(conn, tmp_path / "recordings", now_ms=T0, events=FakeEvents())
    )
    assert 'nightwire_events_subscribed{camera="front"} 1' in rendered
    assert 'nightwire_event_listener_state{camera="front",state="subscribed"} 1' in rendered


def test_go2rtc_state_is_exported(tmp_path):
    class FakeGo2rtc:
        def status(self):
            return {"state": "running", "streams": ["front_main", "front_sub"]}

    conn = fresh(tmp_path)
    rendered = metrics.render(
        metrics.collect(conn, tmp_path / "recordings", now_ms=T0, go2rtc=FakeGo2rtc())
    )
    assert "nightwire_go2rtc_running 1" in rendered
    assert "nightwire_go2rtc_streams 2" in rendered


def test_go2rtc_absent_reports_zero(tmp_path):
    conn = fresh(tmp_path)
    assert "nightwire_go2rtc_running 0" in metrics.render(
        metrics.collect(conn, tmp_path / "recordings", now_ms=T0)
    )


def test_export_store_metrics_are_exported(tmp_path):
    conn = fresh(tmp_path)
    rendered = metrics.render(metrics.collect(conn, tmp_path / "recordings", now_ms=T0))
    assert "nightwire_exports_store_bytes" in rendered
    assert "nightwire_exports_store_cap_bytes" in rendered
    assert "nightwire_exports_queued 0" in rendered


def test_collecting_with_several_cameras(tmp_path):
    conn = fresh(tmp_path, ("front", "yard", "drive"))
    rendered = metrics.render(metrics.collect(conn, tmp_path / "recordings", now_ms=T0))
    assert "nightwire_cameras_total 3" in rendered
    for camera in ("front", "yard", "drive"):
        assert f'nightwire_recorder_recording{{camera="{camera}"}}' in rendered


def test_a_disabled_camera_is_counted_but_not_enabled(tmp_path):
    conn = fresh(tmp_path, ("front", "yard"))
    cameras.update(conn, SECRET, "yard", {"record": False})
    rendered = metrics.render(metrics.collect(conn, tmp_path / "recordings", now_ms=T0))
    assert "nightwire_cameras_total 2" in rendered
    assert "nightwire_cameras_enabled 1" in rendered


def test_exported_names_works_without_a_running_system():
    """Used by the alert-rule test, so it must stand alone."""
    names = metrics.exported_names()
    assert "nightwire_recorder_recording" in names
    assert "nightwire_projected_retention_days" in names
    assert len(names) > 20
