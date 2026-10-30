"""The export command lines."""

from pathlib import Path

from nightwire.export import ffmpeg


def argv(**kw):
    defaults = dict(
        concat_list=Path("/tmp/nvr/export-abc.txt"),
        output=Path("/recordings/_exports/abc_front.mp4"),
        seek_offset_ms=30_000,
        duration_ms=600_000,
    )
    defaults.update(kw)
    return ffmpeg.export_argv(**defaults)


def test_the_seek_is_input_side_not_output_side():
    """Output seeking with -c copy can leave a clip starting mid-GOP.

    the design notes' example command puts -ss after -i, but the behaviour it
    describes - "may start up to one GOP early" - is input seeking.
    """
    args = argv()
    assert args.index("-ss") < args.index("-i")


def test_the_offset_and_duration_are_seconds_with_milliseconds():
    args = argv(seek_offset_ms=30_500, duration_ms=600_250)
    assert args[args.index("-ss") + 1] == "30.500"
    assert args[args.index("-t") + 1] == "600.250"


def test_a_zero_offset_omits_the_seek_entirely():
    """ffmpeg refuses `-ss 0.000` on the concat demuxer.

    Found by exporting a real clip: a cut starting exactly on a segment
    boundary, which "Clip this event" produces routinely, failed outright
    with "could not seek to position 0.000".
    """
    args = argv(seek_offset_ms=0)
    assert "-ss" not in args
    assert args[args.index("-f") + 1] == "concat"


def test_a_tiny_offset_is_still_seeked():
    assert argv(seek_offset_ms=1)[argv(seek_offset_ms=1).index("-ss") + 1] == "0.001"


def test_the_concat_demuxer_is_used_with_safe_off():
    args = argv()
    assert args[args.index("-f") + 1] == "concat"
    assert args[args.index("-safe") + 1] == "0"


def test_the_default_path_copies_rather_than_re_encodes():
    args = argv()
    assert args[args.index("-c") + 1] == "copy"
    assert "libx264" not in args


def test_the_precise_path_re_encodes_with_veryfast():
    args = argv(precise=True)
    assert args[args.index("-c:v") + 1] == "libx264"
    assert args[args.index("-preset") + 1] == "veryfast"
    assert args[args.index("-c:a") + 1] == "aac"
    assert "-c" not in args or args[args.index("-c") + 1] != "copy"


def test_faststart_is_always_set():
    """So the MP4 plays while downloading, and on a phone."""
    for precise in (False, True):
        args = argv(precise=precise)
        assert args[args.index("-movflags") + 1] == "+faststart"


def test_the_output_comes_last():
    assert argv()[-1] == "/recordings/_exports/abc_front.mp4"


def test_existing_output_is_overwritten():
    assert "-y" in argv()


def test_nice_wraps_the_command():
    """A precise re-encode must never starve a recorder."""
    wrapped = ffmpeg.niced(argv())
    assert wrapped[0] == "nice"
    assert wrapped[1] == "-n"
    assert int(wrapped[2]) > 0
    assert wrapped[3] == ffmpeg.FFMPEG
