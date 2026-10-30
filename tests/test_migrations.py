from nightwire import db

EXPECTED_TABLES = {
    "cameras",
    "segments",
    "exports",
    "settings",
    "event_sources",
    "events",
    "schema_migrations",
}


def test_migrations_create_the_full_schema(tmp_path):
    conn = db.connect(tmp_path / "nightwire.db")
    applied = db.migrate(conn)
    assert "001_init.sql" in applied
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert EXPECTED_TABLES <= tables


def test_migrations_are_idempotent(tmp_path):
    path = tmp_path / "nightwire.db"
    conn = db.connect(path)
    first = db.migrate(conn)
    # Every migration shipped so far, in filename order.
    assert first == sorted(first)
    assert "001_init.sql" in first and "002_camera_profiles.sql" in first
    assert db.migrate(conn) == []
    conn.close()

    # And across a restart, which is what a container rebuild looks like.
    reopened = db.connect(path)
    assert db.migrate(reopened) == []


def test_wal_and_foreign_keys_are_on(tmp_path):
    conn = db.connect(tmp_path / "nightwire.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_default_settings_are_seeded(tmp_path):
    conn = db.connect(tmp_path / "nightwire.db")
    db.migrate(conn)
    assert db.get_setting(conn, "retention_days") == "45"
    assert db.get_setting(conn, "max_disk_percent") == "90"
    assert db.get_setting(conn, "target_disk_percent") == "85"
    assert db.get_setting(conn, "segment_seconds") == "60"
    # The export cap added during the 2026-10-27 spec review.
    assert db.get_setting(conn, "max_exports_gb") == "200"


def test_no_cameras_on_a_fresh_install(tmp_path):
    conn = db.connect(tmp_path / "nightwire.db")
    db.migrate(conn)
    assert db.enabled_cameras(conn) == []


def test_camera_profile_columns_exist(tmp_path):
    conn = db.connect(tmp_path / "nightwire.db")
    db.migrate(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(cameras)")}
    assert {
        "main_profile",
        "sub_profile",
        "main_meta",
        "sub_meta",
        "event_source_count",
        "last_probe_at",
        "last_error",
    } <= columns
