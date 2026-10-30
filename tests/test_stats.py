from nightwire import cameras, db
from nightwire.recording import stats

SECRET = "s" * 40
DAY = 86_400_000
NOW = 1_800_000_000_000
GB = 1000**3


def build(tmp_path, per_camera):
    """per_camera: {camera_id: (segment_count, bytes_each, span_days)}"""
    conn = db.connect(tmp_path / "nightwire.db")
    db.migrate(conn)
    for camera_id, (count, size, span_days) in per_camera.items():
        cameras.create(
            conn,
            SECRET,
            camera_id=camera_id,
            name=camera_id,
            host="192.168.1.101",
            main_uri="rtsp://x",
        )
        if count < 2:
            continue
        step = int(span_days * DAY / (count - 1))
        for index in range(count):
            start = NOW - int(span_days * DAY) + index * step
            conn.execute(
                "INSERT INTO segments (camera_id, start_ts, end_ts, duration_ms, path, size_bytes)"
                " VALUES (?,?,?,?,?,?)",
                (camera_id, start, start + 60000, 60000, f"{camera_id}/{index}.ts", size),
            )
    conn.commit()
    return conn


def test_no_segments_means_no_rate(tmp_path):
    conn = build(tmp_path, {"front": (0, 0, 0)})
    assert stats.camera_rates(conn, now_ms=NOW) == []


def test_rate_is_measured_over_the_span_actually_covered(tmp_path):
    # 2 days of footage at 10 GB/day, even though the window is 7 days.
    conn = build(tmp_path, {"front": (3, 10 * GB, 2)})
    [rate] = stats.camera_rates(conn, now_ms=NOW)
    assert rate.camera_id == "front"
    assert round(rate.gb_per_day) == 15  # 30 GB over 2 days
    assert rate.segment_count == 3


def test_a_camera_with_minutes_of_footage_reports_zero_rather_than_a_wild_rate(tmp_path):
    conn = build(tmp_path, {"front": (2, 1 * GB, 0.001)})
    [rate] = stats.camera_rates(conn, now_ms=NOW)
    assert rate.bytes_per_day == 0.0


def test_mbps_matches_the_specs_arithmetic(tmp_path):
    # the design notes: GB per camera per day ~= average Mbps x 10.8
    conn = build(tmp_path, {"front": (3, 21.6 * GB, 2)})  # 64.8 GB over 2 days
    [rate] = stats.camera_rates(conn, now_ms=NOW)
    assert round(rate.gb_per_day, 1) == 32.4
    assert round(rate.mbps, 1) == 3.0


def test_projected_days_uses_the_combined_rate(tmp_path):
    conn = build(tmp_path, {"front": (3, 10 * GB, 2), "yard": (3, 10 * GB, 2)})
    rates = stats.camera_rates(conn, now_ms=NOW)
    # Two cameras at ~15 GB/day each = ~30 GB/day combined. The span is
    # measured from the first segment's start to the last one's end, so it is
    # a minute longer than two days and the rate is a hair under 15.
    assert round(stats.projected_days(300 * GB, rates), 1) == 10.0


def test_projected_days_is_none_without_a_rate():
    assert stats.projected_days(1000, []) is None


def test_projection_warns_when_the_disk_cannot_hold_the_retention(tmp_path):
    conn = build(tmp_path, {"front": (3, 10 * GB, 2)})  # 15 GB/day
    result = stats.projection(
        conn,
        now_ms=NOW,
        total_bytes=100 * GB,
        max_disk_percent=90,
        retention_days={"front": 45},
    )
    assert result["projected_days"] == 6.0  # 90 GB usable / 15 GB per day
    assert len(result["warnings"]) == 1
    warning = result["warnings"][0]
    assert warning["camera_id"] == "front"
    assert warning["retention_days"] == 45
    assert "bitrate cap" in warning["detail"]


def test_projection_is_quiet_when_there_is_room(tmp_path):
    conn = build(tmp_path, {"front": (3, 1 * GB, 2)})  # 1.5 GB/day
    result = stats.projection(
        conn,
        now_ms=NOW,
        total_bytes=8000 * GB,
        max_disk_percent=90,
        retention_days={"front": 45},
    )
    assert result["warnings"] == []
    assert result["projected_days"] > 45


def test_projection_with_no_footage_at_all(tmp_path):
    conn = build(tmp_path, {"front": (0, 0, 0)})
    result = stats.projection(
        conn, now_ms=NOW, total_bytes=8000 * GB, max_disk_percent=90, retention_days={"front": 45}
    )
    assert result["projected_days"] is None
    assert result["warnings"] == []
    assert result["cameras"] == []
