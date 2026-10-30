from nightwire import cameras, db, seed

SECRET = "s" * 40

DOC = {
    "retention_days": 45,
    "max_disk_percent": 90,
    "cameras": [
        {
            "id": "front",
            "name": "Front Door",
            "host": "192.168.1.101",
            "username": "nvr",
            "password": "${CAM_FRONT_PASS}",
            "main_uri": "rtsp://192.168.1.101/onvif-media/media.amp?profile=nvr-main",
            "sub_uri": "rtsp://192.168.1.101/onvif-media/media.amp?profile=nvr-sub",
        }
    ],
}
ENV = {"CAM_FRONT_PASS": "from-env"}


def fresh(tmp_path):
    conn = db.connect(tmp_path / "nightwire.db")
    db.migrate(conn)
    return conn


def test_expand_env():
    assert seed.expand_env("${A}", {"A": "x"}) == "x"
    assert seed.expand_env("pre-${A}-post", {"A": "x"}) == "pre-x-post"
    assert seed.expand_env("plain", {}) == "plain"
    assert seed.expand_env(45, {}) == 45
    assert seed.expand_env(None, {}) is None


def test_missing_env_var_is_an_error_not_an_empty_string():
    try:
        seed.expand_env("${NOPE}", {})
    except seed.SeedError as exc:
        assert "NOPE" in str(exc)
    else:
        raise AssertionError("expected SeedError")


def test_camera_specs_expand_and_default():
    [spec] = seed.camera_specs(DOC, ENV)
    assert spec["id"] == "front"
    assert spec["password"] == "from-env"
    assert spec["onvif_port"] == 80
    assert spec["retention_days"] == 45
    assert spec["record"] is True


def test_retention_falls_back_to_the_document_default():
    doc = {"retention_days": 20, "cameras": [{"name": "A", "host": "1.1.1.1"}]}
    assert seed.camera_specs(doc, {})[0]["retention_days"] == 20


def test_id_is_derived_when_absent():
    doc = {"cameras": [{"name": "Front Door", "host": "10.0.0.1"}]}
    assert seed.camera_specs(doc, {})[0]["id"] == "front"


def test_a_camera_without_a_host_is_an_error():
    try:
        seed.camera_specs({"cameras": [{"name": "A"}]}, {})
    except seed.SeedError as exc:
        assert "host" in str(exc)
    else:
        raise AssertionError("expected SeedError")


def test_a_non_mapping_document_is_an_error():
    for bad in [[], "text", 5]:
        try:
            seed.camera_specs(bad, {})
        except seed.SeedError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_a_non_list_cameras_key_is_an_error():
    try:
        seed.camera_specs({"cameras": {"front": {}}}, {})
    except seed.SeedError:
        return
    raise AssertionError("expected SeedError")


def test_settings_from_picks_only_known_keys():
    assert seed.settings_from(DOC) == {"retention_days": "45", "max_disk_percent": "90"}
    assert seed.settings_from({"nonsense": 1}) == {}


def test_apply_inserts_a_camera_and_settings(tmp_path):
    conn = fresh(tmp_path)
    result = seed.apply(conn, SECRET, DOC, env=ENV)
    assert result["created"] == ["front"]
    assert db.get_setting(conn, "max_disk_percent") == "90"
    assert cameras.get(conn, "front")["host"] == "192.168.1.101"


def test_apply_is_additive_and_leaves_existing_rows_alone(tmp_path):
    conn = fresh(tmp_path)
    seed.apply(conn, SECRET, DOC, env=ENV)
    cameras.update(conn, SECRET, "front", {"name": "Renamed by hand"})
    result = seed.apply(conn, SECRET, DOC, env=ENV)
    assert result["created"] == [] and result["updated"] == []
    assert cameras.get(conn, "front")["name"] == "Renamed by hand"


def test_authoritative_mode_overwrites(tmp_path):
    conn = fresh(tmp_path)
    seed.apply(conn, SECRET, DOC, env=ENV)
    cameras.update(conn, SECRET, "front", {"name": "Renamed by hand"})
    result = seed.apply(conn, SECRET, DOC, env=ENV, authoritative=True)
    assert result["updated"] == ["front"]
    assert cameras.get(conn, "front")["name"] == "Front Door"


def test_a_camera_needing_a_probe_is_skipped_not_inserted(tmp_path):
    conn = fresh(tmp_path)
    doc = {
        "cameras": [
            {
                "id": "front",
                "name": "Front Door",
                "host": "192.168.1.101",
                "profile_main": "nvr-main",
            }
        ]
    }
    result = seed.apply(conn, SECRET, doc, env={})
    assert result["created"] == []
    assert result["skipped"][0]["id"] == "front"
    assert "probe" in result["skipped"][0]["reason"]
    assert cameras.list_cameras(conn) == []


def test_seed_from_config_dir_with_no_file_is_a_no_op(tmp_path):
    conn = fresh(tmp_path)
    assert seed.seed_from_config_dir(conn, SECRET, tmp_path, env={}) is None


def test_seed_from_config_dir_reports_an_error_rather_than_raising(tmp_path):
    conn = fresh(tmp_path)
    (tmp_path / "config.yaml").write_text("cameras:\n  - name: A\n")
    result = seed.seed_from_config_dir(conn, SECRET, tmp_path, env={})
    # Either PyYAML is missing or the camera has no host. Both are reported,
    # never raised, because a bad seed file must not stop the app booting.
    assert result is not None and "error" in result
