"""Retention arithmetic. The spec asks for this to be unit-tested."""

from nightwire import cameras, db
from nightwire.recording import retention
from nightwire.recording.retention import DiskUsage, SegmentRow

DAY = 86_400_000
NOW = 1_800_000_000_000
SECRET = "s" * 40


def seg(seg_id, camera, days_old, size=30_000_000):
    return SegmentRow(
        id=seg_id,
        camera_id=camera,
        start_ts=NOW - int(days_old * DAY),
        size_bytes=size,
        path=f"{camera}/seg{seg_id}.ts",
    )


# ------------------------------------------------------------------- by age


def test_nothing_is_deleted_inside_the_window():
    rows = [seg(1, "front", 10), seg(2, "front", 44.9)]
    assert retention.plan_by_age(rows, NOW, {"front": 45}, 45) == []


def test_segments_past_the_window_go():
    rows = [seg(1, "front", 46), seg(2, "front", 10)]
    doomed = retention.plan_by_age(rows, NOW, {"front": 45}, 45)
    assert [r.id for r in doomed] == [1]


def test_each_camera_uses_its_own_retention():
    rows = [seg(1, "front", 20), seg(2, "yard", 20)]
    doomed = retention.plan_by_age(rows, NOW, {"front": 45, "yard": 14}, 45)
    assert [r.id for r in doomed] == [2]


def test_a_camera_with_no_setting_uses_the_default():
    rows = [seg(1, "ghost", 31)]
    assert [r.id for r in retention.plan_by_age(rows, NOW, {}, 30)] == [1]


def test_age_deletions_are_oldest_first():
    rows = [seg(1, "front", 50), seg(2, "front", 60), seg(3, "front", 55)]
    assert [r.id for r in retention.plan_by_age(rows, NOW, {"front": 45}, 45)] == [2, 3, 1]


# ------------------------------------------------------------------ by disk


def test_the_guard_stays_out_of_the_way_below_the_ceiling():
    usage = DiskUsage(total_bytes=1000, used_bytes=800, free_bytes=200)
    rows = [seg(1, "front", 1)]
    assert retention.plan_by_disk(rows, usage, 90, 85) == []


def test_the_guard_deletes_oldest_first_across_all_cameras():
    usage = DiskUsage(total_bytes=1000, used_bytes=950, free_bytes=50)
    rows = [
        SegmentRow(1, "front", NOW - 3 * DAY, 30, "front/1.ts"),
        SegmentRow(2, "yard", NOW - 5 * DAY, 30, "yard/2.ts"),
        SegmentRow(3, "drive", NOW - 1 * DAY, 30, "drive/3.ts"),
    ]
    doomed = retention.plan_by_disk(rows, usage, 90, 85)
    # 950 -> needs to reach 850: two segments of 30 is not enough, three is.
    assert [r.id for r in doomed] == [2, 1, 3]


def test_the_guard_stops_as_soon_as_it_reaches_target():
    usage = DiskUsage(total_bytes=1000, used_bytes=910, free_bytes=90)
    rows = [SegmentRow(i, "front", NOW - i * DAY, 40, f"front/{i}.ts") for i in range(1, 6)]
    doomed = retention.plan_by_disk(rows, usage, 90, 85)
    # Needs 60 bytes freed; one 40-byte segment is short, two is enough.
    assert len(doomed) == 2


def test_age_deletions_count_toward_the_disk_target():
    usage = DiskUsage(total_bytes=1000, used_bytes=950, free_bytes=50)
    rows = [SegmentRow(1, "front", NOW - 1 * DAY, 30, "front/1.ts")]
    # 100 bytes already being freed by the age pass takes us under target, so
    # the guard must not pile on.
    assert retention.plan_by_disk(rows, usage, 90, 85, already_freeing=100) == []


def test_an_empty_volume_reading_does_not_divide_by_zero():
    usage = DiskUsage(total_bytes=0, used_bytes=0, free_bytes=0)
    assert usage.used_percent == 0.0
    assert retention.plan_by_disk([seg(1, "front", 1)], usage, 90, 85) == []


# --------------------------------------------------------------- combined


def test_plan_does_not_count_a_segment_twice():
    usage = DiskUsage(total_bytes=1000, used_bytes=990, free_bytes=10)
    rows = [seg(1, "front", 99, size=100), seg(2, "front", 1, size=100)]
    decided = retention.plan(rows, usage, now_ms=NOW, retention_days={"front": 45})
    ids = [r.id for r in decided.all]
    assert len(ids) == len(set(ids))
    assert decided.bytes_freed == sum(r.size_bytes for r in decided.all)


def test_plan_is_falsey_when_there_is_nothing_to_do():
    usage = DiskUsage(total_bytes=1000, used_bytes=100, free_bytes=900)
    assert not retention.plan([seg(1, "front", 1)], usage, now_ms=NOW, retention_days={"front": 45})


# -------------------------------------------------------------- the export cap


def test_export_cap_blocks_rather_than_deletes(tmp_path):
    exports = tmp_path / "_exports"
    exports.mkdir()
    (exports / "clip.mp4").write_bytes(b"x" * 2048)

    exceeded, used, limit = retention.export_cap_exceeded(tmp_path, 0.000001)
    assert exceeded is True
    assert used == 2048
    assert limit == 1000
    # And nothing was removed: exports are evidence.
    assert (exports / "clip.mp4").is_file()


def test_export_cap_not_exceeded_under_the_limit(tmp_path):
    (tmp_path / "_exports").mkdir()
    exceeded, used, _ = retention.export_cap_exceeded(tmp_path, 200)
    assert exceeded is False and used == 0


def test_export_store_bytes_with_no_directory(tmp_path):
    assert retention.export_store_bytes(tmp_path) == 0


# ------------------------------------------------------------------- enforce


def build(tmp_path, days_old, camera="front", retention_days=45):
    conn = db.connect(tmp_path / "nightwire.db")
    db.migrate(conn)
    cameras.create(
        conn,
        SECRET,
        camera_id=camera,
        name=camera,
        host="192.168.1.101",
        main_uri="rtsp://x",
        retention_days=retention_days,
    )
    recordings = tmp_path / "recordings"
    created = []
    for index, age in enumerate(days_old, start=1):
        relative = f"{camera}/2026/10/27/14/seg{index}.ts"
        absolute = recordings / relative
        absolute.parent.mkdir(parents=True, exist_ok=True)
        absolute.write_bytes(b"x" * 1024)
        thumb = recordings / "_thumbs" / camera / f"seg{index}.jpg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"t")
        conn.execute(
            "INSERT INTO segments (camera_id, start_ts, end_ts, duration_ms, path, size_bytes)"
            " VALUES (?,?,?,?,?,?)",
            (camera, NOW - int(age * DAY), NOW - int(age * DAY) + 60000, 60000, relative, 1024),
        )
        created.append(absolute)
    conn.commit()
    return conn, recordings, created


def test_enforce_deletes_expired_files_rows_and_thumbnails(tmp_path):
    conn, recordings, files = build(tmp_path, [50, 1])
    result = retention.enforce(conn, recordings, now_ms=NOW)
    assert result["deleted_segments"] == 1
    assert result["deleted_by_age"] == 1
    assert result["deleted_files"] == 1
    assert not files[0].exists()
    assert files[1].exists()
    assert not (recordings / "_thumbs" / "front" / "seg1.jpg").exists()
    assert (recordings / "_thumbs" / "front" / "seg2.jpg").exists()
    assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 1


def test_enforce_never_touches_exports(tmp_path):
    conn, recordings, _ = build(tmp_path, [50])
    exports = recordings / "_exports"
    exports.mkdir(parents=True, exist_ok=True)
    clip = exports / "saved.mp4"
    clip.write_bytes(b"evidence")
    retention.enforce(conn, recordings, now_ms=NOW)
    assert clip.is_file()


def test_enforce_removes_events_pointing_at_deleted_footage(tmp_path):
    conn, recordings, _ = build(tmp_path, [50, 1])
    snapshot = recordings / "_events" / "front" / "1.jpg"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_bytes(b"s")
    conn.execute(
        "INSERT INTO event_sources (id, camera_id, topic, label, color, kind)"
        " VALUES (1, 'front', 't', 'Porch', '#FF2BD6', 'span')"
    )
    conn.execute(
        "INSERT INTO events (camera_id, source_id, start_ts, snapshot)"
        " VALUES ('front', 1, ?, '_events/front/1.jpg')",
        (NOW - 50 * DAY,),
    )
    conn.execute(
        "INSERT INTO events (camera_id, source_id, start_ts) VALUES ('front', 1, ?)",
        (NOW - 1 * DAY,),
    )
    conn.commit()

    result = retention.enforce(conn, recordings, now_ms=NOW)
    assert result["deleted_events"] == 1
    assert not snapshot.exists()
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_enforce_prunes_empty_hour_folders(tmp_path):
    conn, recordings, _ = build(tmp_path, [50])
    retention.enforce(conn, recordings, now_ms=NOW)
    assert not (recordings / "front" / "2026").exists()


def test_enforce_tolerates_a_row_whose_file_is_gone(tmp_path):
    conn, recordings, files = build(tmp_path, [50])
    files[0].unlink()
    result = retention.enforce(conn, recordings, now_ms=NOW)
    assert result["missing_files"] == 1
    assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 0


def test_enforce_respects_a_per_camera_retention_override(tmp_path):
    conn, recordings, files = build(tmp_path, [20], retention_days=14)
    retention.enforce(conn, recordings, now_ms=NOW)
    assert not files[0].exists()


def test_enforce_does_nothing_when_nothing_is_expired(tmp_path):
    conn, recordings, files = build(tmp_path, [1, 2])
    result = retention.enforce(conn, recordings, now_ms=NOW)
    assert result["deleted_segments"] == 0
    assert all(f.exists() for f in files)
