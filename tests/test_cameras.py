from pathlib import Path

from nightwire import cameras, crypto, db

SECRET = "s" * 40


def fresh(tmp_path: Path):
    conn = db.connect(tmp_path / "nightwire.db")
    db.migrate(conn)
    return conn


def add(conn, camera_id="front", name="Front Door", host="192.168.1.101", **kw):
    return cameras.create(
        conn,
        SECRET,
        camera_id=camera_id,
        name=name,
        host=host,
        main_uri=kw.pop("main_uri", f"rtsp://{host}/onvif-media/media.amp?profile=nvr-main"),
        **kw,
    )


def test_create_and_read(tmp_path):
    conn = fresh(tmp_path)
    camera = add(conn)
    assert camera["id"] == "front"
    assert camera["name"] == "Front Door"
    assert camera["record"] is True
    assert camera["retention_days"] == 45
    assert cameras.get(conn, "front")["host"] == "192.168.1.101"


def test_list_is_ordered_by_sort_order(tmp_path):
    conn = fresh(tmp_path)
    add(conn, "front", "Front Door", "192.168.1.101")
    add(conn, "drive", "Driveway", "192.168.1.102")
    add(conn, "yard", "Backyard", "192.168.1.103")
    assert [c["id"] for c in cameras.list_cameras(conn)] == ["front", "drive", "yard"]


def test_duplicate_id_is_rejected(tmp_path):
    conn = fresh(tmp_path)
    add(conn)
    try:
        add(conn)
    except cameras.CameraError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("expected CameraError")


def test_bad_ids_are_rejected(tmp_path):
    for bad in ["", "A", "Front", "front door", "front/../etc", "f", "x" * 40, "-front"]:
        try:
            cameras.validate_id(bad)
        except cameras.CameraError:
            continue
        raise AssertionError(f"accepted bad id {bad!r}")


def test_good_ids_are_accepted():
    for good in ["front", "drive-2", "yard", "cam01"]:
        assert cameras.validate_id(good) == good


def test_missing_main_uri_is_rejected(tmp_path):
    conn = fresh(tmp_path)
    try:
        cameras.create(conn, SECRET, camera_id="front", name="Front", host="10.0.0.1", main_uri="")
    except cameras.CameraError as exc:
        assert "main stream" in str(exc)
    else:
        raise AssertionError("expected CameraError")


def test_zero_retention_is_rejected(tmp_path):
    conn = fresh(tmp_path)
    try:
        add(conn, retention_days=0)
    except cameras.CameraError as exc:
        assert "at least 1 day" in str(exc)
    else:
        raise AssertionError("expected CameraError")


def test_unknown_camera_raises_not_found(tmp_path):
    conn = fresh(tmp_path)
    try:
        cameras.get(conn, "nope")
    except cameras.NotFound:
        return
    raise AssertionError("expected NotFound")


def test_password_is_never_in_the_public_shape(tmp_path):
    if not crypto.available():
        return
    conn = fresh(tmp_path)
    camera = add(conn, username="nvr", password="super-secret")
    assert "password_enc" not in camera
    assert camera["has_password"] is True
    assert "super-secret" not in repr(camera)
    # And the stored form is not the plaintext.
    row = cameras.get_row(conn, "front")
    assert row["password_enc"] and "super-secret" not in row["password_enc"]


def test_credentials_round_trip(tmp_path):
    if not crypto.available():
        return
    conn = fresh(tmp_path)
    add(conn, username="nvr", password="super-secret")
    assert cameras.credentials(conn, "front", SECRET) == ("nvr", "super-secret")


def test_camera_without_a_password(tmp_path):
    conn = fresh(tmp_path)
    camera = add(conn)
    assert camera["has_password"] is False


def test_update_editable_fields(tmp_path):
    conn = fresh(tmp_path)
    add(conn)
    updated = cameras.update(conn, SECRET, "front", {"name": "Porch", "retention_days": 30})
    assert updated["name"] == "Porch"
    assert updated["retention_days"] == 30


def test_update_rejects_protected_columns(tmp_path):
    conn = fresh(tmp_path)
    add(conn)
    for forbidden in [{"id": "other"}, {"password_enc": "x"}, {"created_at": "now"}]:
        try:
            cameras.update(conn, SECRET, "front", forbidden)
        except cameras.CameraError as exc:
            assert "Cannot change" in str(exc)
            continue
        raise AssertionError(f"accepted {forbidden}")


def test_record_toggle(tmp_path):
    conn = fresh(tmp_path)
    add(conn)
    assert cameras.update(conn, SECRET, "front", {"record": False})["record"] is False
    assert db.enabled_cameras(conn) == []
    assert cameras.update(conn, SECRET, "front", {"record": True})["record"] is True
    assert len(db.enabled_cameras(conn)) == 1


def test_clear_password(tmp_path):
    if not crypto.available():
        return
    conn = fresh(tmp_path)
    add(conn, username="nvr", password="secret")
    assert cameras.update(conn, SECRET, "front", {}, clear_password=True)["has_password"] is False


def test_record_probe_stores_device_info(tmp_path):
    conn = fresh(tmp_path)
    add(conn)
    cameras.record_probe(
        conn,
        "front",
        device={"model": "M1234-LVE", "firmware": "10.12.338", "serial": "ABC"},
        main_meta={"summary": "nvr-main"},
        event_source_count=5,
    )
    camera = cameras.get(conn, "front")
    assert camera["model"] == "M1234-LVE"
    assert camera["firmware"] == "10.12.338"
    assert camera["event_source_count"] == 5
    assert camera["main_stream"] == {"summary": "nvr-main"}
    assert camera["last_probe_at"]


def test_record_probe_stores_an_error(tmp_path):
    conn = fresh(tmp_path)
    add(conn)
    cameras.record_probe(conn, "front", error="timed out")
    assert cameras.get(conn, "front")["last_error"] == "timed out"


def test_delete_keeps_footage_by_default(tmp_path):
    conn = fresh(tmp_path)
    add(conn)
    recordings = tmp_path / "recordings"
    footage = recordings / "front" / "2026" / "10" / "27" / "14"
    footage.mkdir(parents=True)
    (footage / "20261027T143200.ts").write_bytes(b"x")

    result = cameras.delete(conn, "front", recordings_dir=recordings)
    assert result["recordings_deleted"] == 0
    assert footage.exists()
    try:
        cameras.get(conn, "front")
    except cameras.NotFound:
        pass
    else:
        raise AssertionError("camera row should be gone")


def test_delete_can_remove_footage_when_asked(tmp_path):
    conn = fresh(tmp_path)
    add(conn)
    recordings = tmp_path / "recordings"
    (recordings / "front").mkdir(parents=True)
    (recordings / "_thumbs" / "front").mkdir(parents=True)

    result = cameras.delete(conn, "front", recordings_dir=recordings, delete_recordings=True)
    assert result["recordings_deleted"] == 2
    assert not (recordings / "front").exists()
    assert not (recordings / "_thumbs" / "front").exists()


def test_delete_removes_dependent_rows(tmp_path):
    conn = fresh(tmp_path)
    add(conn)
    conn.execute(
        "INSERT INTO segments (camera_id, start_ts, end_ts, duration_ms, path, size_bytes)"
        " VALUES ('front', 1, 2, 1000, 'front/x.ts', 10)"
    )
    conn.execute(
        "INSERT INTO event_sources (camera_id, topic, label, color, kind)"
        " VALUES ('front', 'topic', 'Porch', '#FF2BD6', 'span')"
    )
    conn.commit()
    cameras.delete(conn, "front")
    assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM event_sources").fetchone()[0] == 0


def test_suggest_id_avoids_existing(tmp_path):
    conn = fresh(tmp_path)
    add(conn)
    assert cameras.suggest_id(conn, "Front Door") != "front"


# ------------------------------ credentials never live in the URI column -----


def test_credentials_pasted_into_a_uri_are_moved_out(tmp_path):
    """The design's own manual template is rtsp://USER:PASS@IP/...

    Storing that verbatim would put a plaintext camera password in the
    database and in every backup of it.
    """
    conn = fresh(tmp_path)
    camera = add(conn, main_uri="rtsp://root:hunter2@192.168.1.101/media.amp")
    assert camera["main_uri"] == "rtsp://192.168.1.101/media.amp"
    assert camera["username"] == "root"
    assert camera["has_password"]

    row = cameras.get_row(conn, "front")
    assert "hunter2" not in row["main_uri"]
    assert "hunter2" not in (row["password_enc"] or "")
    assert crypto.decrypt(SECRET, row["password_enc"]) == "hunter2"


def test_explicit_credentials_win_over_embedded_ones(tmp_path):
    conn = fresh(tmp_path)
    camera = add(
        conn,
        main_uri="rtsp://embedded:embedded@192.168.1.101/media.amp",
        username="nvr",
        password="chosen",
    )
    assert camera["username"] == "nvr"
    row = cameras.get_row(conn, "front")
    assert crypto.decrypt(SECRET, row["password_enc"]) == "chosen"


def test_credentials_are_stripped_from_a_substream_uri_too(tmp_path):
    conn = fresh(tmp_path)
    camera = add(conn, sub_uri="rtsp://root:hunter2@192.168.1.101/media.amp?profile=sub")
    assert camera["sub_uri"] == "rtsp://192.168.1.101/media.amp?profile=sub"


def test_credentials_are_stripped_on_update(tmp_path):
    conn = fresh(tmp_path)
    add(conn)
    camera = cameras.update(
        conn, SECRET, "front", {"main_uri": "rtsp://root:hunter2@192.168.1.101/new.amp"}
    )
    assert camera["main_uri"] == "rtsp://192.168.1.101/new.amp"


def test_authenticated_uri_injects_the_stored_password(tmp_path):
    conn = fresh(tmp_path)
    add(conn, username="nvr", password="p@ss/word")
    row = cameras.get_row(conn, "front")
    out = cameras.authenticated_uri(row, SECRET)
    assert out.startswith("rtsp://nvr:p%40ss%2Fword@192.168.1.101/")
    assert cameras.to_public(row)["main_uri"] == row["main_uri"]


def test_authenticated_uri_without_a_username_is_unchanged(tmp_path):
    conn = fresh(tmp_path)
    add(conn)
    row = cameras.get_row(conn, "front")
    assert cameras.authenticated_uri(row, SECRET) == row["main_uri"]


def test_authenticated_uri_with_the_wrong_secret_degrades(tmp_path):
    """A rotated NVR_SECRET_KEY must not take the recorder down with it."""
    conn = fresh(tmp_path)
    add(conn, username="nvr", password="p")
    row = cameras.get_row(conn, "front")
    out = cameras.authenticated_uri(row, "x" * 40)
    assert out == row["main_uri"]


def test_authenticated_uri_for_a_missing_substream(tmp_path):
    conn = fresh(tmp_path)
    add(conn, username="nvr", password="p")
    row = cameras.get_row(conn, "front")
    assert cameras.authenticated_uri(row, SECRET, "sub") == ""
