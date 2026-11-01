"""go2rtc stream naming and config generation. Pure, so it runs anywhere."""

from nightwire import cameras, db
from nightwire.live import config

SECRET = "s" * 40


def fresh(tmp_path, specs):
    """specs: [(id, main_uri, sub_uri)]"""
    conn = db.connect(tmp_path / "nightwire.db")
    db.migrate(conn)
    for camera_id, main, sub in specs:
        cameras.create(
            conn,
            SECRET,
            camera_id=camera_id,
            name=camera_id,
            host="192.168.1.101",
            main_uri=main,
            sub_uri=sub,
        )
    return conn


def rows(conn):
    return list(conn.execute("SELECT * FROM cameras ORDER BY sort_order, id"))


def test_stream_names():
    assert config.stream_name("front", "main") == "front_main"
    assert config.stream_name("front", "sub") == "front_sub"


def test_stream_name_rejects_anything_else():
    try:
        config.stream_name("front", "quality")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_parse_stream_name_round_trips():
    for camera_id in ["front", "drive-2", "yard"]:
        for which in ["main", "sub"]:
            name = config.stream_name(camera_id, which)
            assert config.parse_stream_name(name) == (camera_id, which)


def test_parse_stream_name_ignores_foreign_streams():
    """A stream someone added by hand in go2rtc must not be touched."""
    for name in ["doorbell", "", "_main", "_sub", "camera1"]:
        assert config.parse_stream_name(name) is None


def test_source_url_disables_the_backchannel():
    assert config.source_url("rtsp://x/stream") == "rtsp://x/stream#backchannel=0"


def test_source_url_leaves_an_explicit_backchannel_alone():
    given = "rtsp://x/stream#backchannel=1"
    assert config.source_url(given) == given


def test_source_url_on_nothing():
    assert config.source_url("") == ""


def test_two_streams_per_camera(tmp_path):
    conn = fresh(tmp_path, [("front", "rtsp://a/main", "rtsp://a/sub")])
    specs = config.streams_for(rows(conn))
    assert [s.name for s in specs] == ["front_main", "front_sub"]
    assert all("#backchannel=0" in s.source for s in specs)


def test_a_camera_without_a_substream_gets_one_stream(tmp_path):
    conn = fresh(tmp_path, [("front", "rtsp://a/main", None)])
    assert [s.name for s in config.streams_for(rows(conn))] == ["front_main"]


def test_grid_prefers_the_substream(tmp_path):
    conn = fresh(tmp_path, [("front", "rtsp://a/main", "rtsp://a/sub")])
    [camera] = cameras.list_cameras(conn)
    assert config.grid_stream_for(camera) == "front_sub"


def test_grid_falls_back_to_main(tmp_path):
    conn = fresh(tmp_path, [("front", "rtsp://a/main", None)])
    [camera] = cameras.list_cameras(conn)
    assert config.grid_stream_for(camera) == "front_main"


def test_a_camera_with_recording_off_is_still_published(tmp_path):
    """Live view and recording are independent switches."""
    conn = fresh(tmp_path, [("front", "rtsp://a/main", "rtsp://a/sub")])
    cameras.update(conn, SECRET, "front", {"record": False})
    assert len(config.streams_for(rows(conn))) == 2


def test_config_binds_api_and_rtsp_to_localhost(tmp_path):
    conn = fresh(tmp_path, [("front", "rtsp://a/main", "rtsp://a/sub")])
    document = config.build_config(rows(conn))
    assert document["api"]["listen"] == "127.0.0.1:1984"
    assert document["rtsp"]["listen"] == "127.0.0.1:8554"


def test_webrtc_listens_on_every_interface(tmp_path):
    """Media cannot flow if 8555 is localhost-only."""
    conn = fresh(tmp_path, [("front", "rtsp://a/main", None)])
    assert config.build_config(rows(conn))["webrtc"]["listen"] == ":8555"


def test_config_lists_every_stream(tmp_path):
    conn = fresh(
        tmp_path,
        [("front", "rtsp://a/main", "rtsp://a/sub"), ("yard", "rtsp://b/main", None)],
    )
    streams = config.build_config(rows(conn))["streams"]
    assert set(streams) == {"front_main", "front_sub", "yard_main"}


def parse_block(text: str) -> dict:
    """A deliberately dumb reader for the two-level block YAML we emit.

    Not a YAML parser: it exists so the tests assert on the exact shape
    written to disk rather than on a round trip through one.
    """
    out: dict = {}
    section = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line.startswith("  "):
            key, _, inline = line.partition(":")
            if inline.strip() in ("", "{}"):
                section = out.setdefault(key, {})
            else:
                out[key] = inline.strip().strip('"')
                section = None
            continue
        assert section is not None, line
        key, _, value = line.strip().partition(":")
        section[key] = value.strip().strip('"')
    return out


def test_config_with_no_cameras_is_still_valid():
    document = config.build_config([])
    assert document["streams"] == {}
    parsed = parse_block(config.to_yaml(document))
    assert parsed["api"]["listen"] == "127.0.0.1:1984"


def test_the_config_is_block_style_not_json():
    """go2rtc patches this file itself when a stream is added through its
    API, and its patcher cannot handle a flow-style document: it answered
    every registration with 400 and the supervisor restarted it in a loop."""
    text = config.to_yaml(config.build_config([]))
    assert not text.lstrip().startswith("{")
    assert "\napi:\n" in "\n" + text
    assert '  listen: "127.0.0.1:1984"' in text


def test_yaml_output_quotes_awkward_urls(tmp_path):
    """RTSP URLs carry ?, &, @ and #. Unquoted, `#` starts a YAML comment
    and would silently truncate the source."""
    awkward = "rtsp://nvr:p%40ss@192.168.1.101/onvif-media/media.amp?profile=nvr-main&x=1"
    conn = fresh(tmp_path, [("front", awkward, None)])
    text = config.to_yaml(config.build_config(rows(conn)))
    expected = "rtsp://192.168.1.101/onvif-media/media.amp?profile=nvr-main&x=1#backchannel=0"
    assert f'  front_main: "{expected}"' in text
    # Credentials were moved out of the URI at creation, so the config built
    # without the secret is credential-free.
    assert parse_block(text)["streams"]["front_main"] == expected


def test_a_quote_or_backslash_in_a_url_is_escaped():
    document = {"streams": {"odd_main": 'rtsp://h/a"b\\c'}}
    line = config.to_yaml(document).strip()
    assert line == 'streams:\n  odd_main: "rtsp://h/a\\"b\\\\c"'.replace("\\n", "\n")


def test_a_source_url_carries_credentials_only_when_given_the_secret(tmp_path):
    """go2rtc can only authenticate by credentials in the URL."""
    conn = fresh(tmp_path, [("front", "rtsp://nvr:p%40ss@192.168.1.101/media.amp", None)])
    rows_ = rows(conn)

    without = config.build_config(rows_)["streams"]["front_main"]
    assert "@" not in without

    with_secret = config.build_config(rows_, secret=SECRET)["streams"]["front_main"]
    # Re-encoded, not double-encoded: the `@` in the password stays %40.
    assert with_secret == "rtsp://nvr:p%40ss@192.168.1.101/media.amp#backchannel=0"


def test_a_camera_with_no_username_gets_a_plain_url(tmp_path):
    conn = fresh(tmp_path, [("yard", "rtsp://192.168.1.102/media.amp", None)])
    assert config.build_config(rows(conn), secret=SECRET)["streams"]["yard_main"] == (
        "rtsp://192.168.1.102/media.amp#backchannel=0"
    )


def test_urls():
    assert config.api_url() == "http://127.0.0.1:1984"
    assert config.api_url("api/streams") == "http://127.0.0.1:1984/api/streams"
    assert config.ws_url("front_sub") == "ws://127.0.0.1:1984/api/ws?src=front_sub"
    assert config.frame_url("front_sub").endswith("api/frame.jpeg?src=front_sub")


def test_an_empty_stream_list_is_block_style_not_flow():
    """`streams: {}` is what a config written before any camera looked like.

    go2rtc patches this file itself when it accepts a stream through its
    API, and its patcher rejects the flow-style empty map with
    `400 yaml: did not find expected key`. Adding the first camera to a
    running container therefore left live video dead until the next restart.
    """
    text = config.to_yaml(config.build_config([]))
    assert "streams: {}" not in text
    assert "\nstreams:\n" in text or text.endswith("streams:\n")
    parsed = parse_block(text)
    assert parsed["streams"] == {}


def test_an_empty_config_still_carries_the_listeners():
    parsed = parse_block(config.to_yaml(config.build_config([])))
    assert parsed["api"]["listen"] == "127.0.0.1:1984"
    assert parsed["webrtc"]["listen"] == ":8555"
