from pathlib import Path

from nightwire.recording import ffmpeg


def argv(**kw):
    defaults = dict(
        rtsp_uri="rtsp://192.168.1.101/onvif-media/media.amp?profile=nvr-main",
        recordings_dir=Path("/recordings"),
        camera_id="front",
        segment_seconds=60,
        segment_list=Path("/tmp/nvr/front.csv"),
    )
    defaults.update(kw)
    return ffmpeg.record_argv(**defaults)


def pair(args, flag):
    """The value following a flag, or None."""
    return args[args.index(flag) + 1] if flag in args else None


def test_stream_copy_never_re_encodes():
    args = argv()
    assert pair(args, "-c") == "copy"
    assert "libx264" not in args


def test_rtsp_over_tcp_with_a_socket_timeout():
    args = argv()
    assert pair(args, "-rtsp_transport") == "tcp"
    assert pair(args, "-timeout") == str(ffmpeg.RTSP_TIMEOUT_US)


def test_segment_muxer_settings():
    args = argv()
    assert pair(args, "-f") == "segment"
    assert pair(args, "-segment_time") == "60"
    assert pair(args, "-segment_atclocktime") == "1"
    assert pair(args, "-reset_timestamps") == "1"
    assert pair(args, "-strftime") == "1"


def test_segment_list_is_live_not_cached():
    """The whole point of the spec review: +cache writes nothing until exit."""
    args = argv()
    assert pair(args, "-segment_list_flags") == "+live"
    assert pair(args, "-segment_list_type") == "csv"
    assert pair(args, "-segment_list") == "/tmp/nvr/front.csv"


def test_no_segment_list_flags_without_a_list():
    args = argv(segment_list=None)
    assert "-segment_list" not in args
    assert "-segment_list_flags" not in args


def test_audio_is_optional_so_a_silent_camera_still_records():
    args = argv()
    assert "0:a?" in args
    assert args.count("-map") == 2


def test_audio_can_be_dropped_entirely():
    args = argv(with_audio=False)
    assert "0:a?" not in args
    assert args.count("-map") == 1


def test_output_is_the_strftime_pattern_and_comes_last():
    args = argv()
    assert args[-1] == "/recordings/front/%Y/%m/%d/%H/%Y%m%dT%H%M%S.ts"


def test_input_precedes_output_flags():
    args = argv()
    assert args.index("-i") < args.index("-c")


def test_probe_argv_asks_for_duration_and_stream_types():
    args = ffmpeg.probe_argv(Path("/recordings/front/x.ts"))
    assert args[0] == "ffprobe"
    assert "format=duration:stream=codec_type" in args
    assert "json" in args


def test_parse_probe_reads_duration_and_audio():
    out = '{"streams":[{"codec_type":"video"},{"codec_type":"audio"}],"format":{"duration":"60.024000"}}'
    assert ffmpeg.parse_probe(out) == {"duration_ms": 60024, "has_audio": True}


def test_parse_probe_video_only():
    out = '{"streams":[{"codec_type":"video"}],"format":{"duration":"59.9"}}'
    assert ffmpeg.parse_probe(out) == {"duration_ms": 59900, "has_audio": False}


def test_parse_probe_handles_bytes():
    assert ffmpeg.parse_probe(b'{"format":{"duration":"1.5"}}')["duration_ms"] == 1500


def test_parse_probe_on_junk_reports_zero_rather_than_guessing():
    for junk in ["", "not json", "{}", '{"format":{}}', '{"format":{"duration":"N/A"}}']:
        assert ffmpeg.parse_probe(junk)["duration_ms"] == 0


def test_thumbnail_argv_decodes_keyframes_only():
    args = ffmpeg.thumbnail_argv(Path("/a/x.ts"), Path("/b/x.jpg"))
    assert pair(args, "-skip_frame") == "nokey"
    assert pair(args, "-frames:v") == "1"
    assert pair(args, "-vf") == "scale=320:-1"
    assert args[-1] == "/b/x.jpg"
