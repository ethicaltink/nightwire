from datetime import datetime, timezone
from pathlib import Path

from nightwire.recording import paths

REC = Path("/recordings")


def test_segment_pattern_is_the_strftime_layout():
    assert paths.segment_pattern(REC, "front") == "/recordings/front/%Y/%m/%d/%H/%Y%m%dT%H%M%S.ts"


def test_hour_dir():
    moment = datetime(2026, 10, 27, 14, 32, 5)
    assert paths.hour_dir(REC, "front", moment) == Path("/recordings/front/2026/10/27/14")


def test_precreate_covers_this_hour_and_the_next_two():
    moment = datetime(2026, 10, 27, 22, 59, 0)
    dirs = paths.hour_dirs_to_precreate(REC, "front", moment)
    assert [d.name for d in dirs] == ["22", "23", "00"]
    # And it rolls the date, which is the case that breaks a naive version.
    assert dirs[2] == Path("/recordings/front/2026/10/28/00")


def test_reserved_dirs():
    assert paths.thumbs_dir(REC, "front") == Path("/recordings/_thumbs/front")
    assert paths.events_dir(REC, "front") == Path("/recordings/_events/front")
    assert paths.exports_dir(REC) == Path("/recordings/_exports")


def test_thumb_path_mirrors_the_segment_name():
    segment = Path("/recordings/front/2026/10/27/14/20261027T143200.ts")
    assert paths.thumb_path(REC, "front", segment) == Path(
        "/recordings/_thumbs/front/20261027T143200.jpg"
    )


def test_is_segment():
    assert paths.is_segment(Path("20261027T143200.ts"))
    assert not paths.is_segment(Path("20261027T143200.mp4"))
    assert not paths.is_segment(Path("notatimestamp.ts"))
    assert not paths.is_segment(Path("2026102T143200.ts"))


def test_parse_filename():
    assert paths.parse_filename("20261027T143200.ts") == datetime(2026, 10, 27, 14, 32, 0)
    assert paths.parse_filename("nope.ts") is None
    assert paths.parse_filename("20261347T143200.ts") is None  # month 13


def test_relative_and_absolute_round_trip(tmp_path):
    segment = tmp_path / "front" / "2026" / "10" / "27" / "14" / "20261027T143200.ts"
    segment.parent.mkdir(parents=True)
    segment.write_bytes(b"x")
    relative = paths.relative_path(tmp_path, segment)
    assert relative == "front/2026/10/27/14/20261027T143200.ts"
    assert paths.absolute_path(tmp_path, relative) == segment


def test_camera_id_from_relative():
    assert paths.camera_id_from_relative("front/2026/10/27/14/x.ts") == "front"
    assert paths.camera_id_from_relative("_thumbs/front/x.jpg") is None
    assert paths.camera_id_from_relative("_exports/abc.mp4") is None


def test_segment_start_ms_uses_the_filename_in_the_given_zone():
    path = Path("20261027T143200.ts")
    utc = paths.segment_start_ms(path, timezone.utc)
    assert utc == int(datetime(2026, 10, 27, 14, 32, 0, tzinfo=timezone.utc).timestamp() * 1000)


def test_segment_start_ms_prefers_mtime_when_the_filename_is_wildly_wrong():
    path = Path("19700101T000000.ts")
    mtime = datetime(2026, 10, 27, 14, 32, 0, tzinfo=timezone.utc).timestamp()
    assert paths.segment_start_ms(path, timezone.utc, mtime=mtime) == int(mtime * 1000)


def test_segment_start_ms_keeps_the_filename_for_a_small_disagreement():
    # A few minutes of clock drift must not move a segment's timestamp.
    naive = datetime(2026, 10, 27, 14, 32, 0, tzinfo=timezone.utc)
    mtime = naive.timestamp() + 120
    assert paths.segment_start_ms(Path("20261027T143200.ts"), timezone.utc, mtime=mtime) == int(
        naive.timestamp() * 1000
    )


def test_iter_segment_files_is_chronological(tmp_path):
    for name in ["20261027T143300.ts", "20261027T143100.ts", "20261027T143200.ts", "junk.ts"]:
        target = tmp_path / "front" / "2026" / "10" / "27" / "14" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
    found = [p.name for p in paths.iter_segment_files(tmp_path, "front")]
    assert found == ["20261027T143100.ts", "20261027T143200.ts", "20261027T143300.ts"]


def test_iter_segment_files_on_a_missing_camera(tmp_path):
    assert list(paths.iter_segment_files(tmp_path, "nope")) == []


def test_prune_empty_dirs_walks_up_and_stops_at_the_volume(tmp_path):
    hour = tmp_path / "front" / "2026" / "10" / "27" / "14"
    hour.mkdir(parents=True)
    removed = paths.prune_empty_dirs(hour, tmp_path)
    assert removed == 5  # hour, day, month, year, camera
    assert tmp_path.is_dir()
    assert not (tmp_path / "front").exists()


def test_prune_stops_at_a_non_empty_parent(tmp_path):
    hour = tmp_path / "front" / "2026" / "10" / "27" / "14"
    hour.mkdir(parents=True)
    keeper = tmp_path / "front" / "2026" / "10" / "27" / "15"
    keeper.mkdir()
    (keeper / "20261027T150000.ts").write_bytes(b"x")
    removed = paths.prune_empty_dirs(hour, tmp_path)
    assert removed == 1
    assert keeper.is_dir()


def test_prune_never_escapes_the_volume(tmp_path):
    outside = tmp_path.parent
    assert paths.prune_empty_dirs(outside, tmp_path) == 0
    assert outside.is_dir()
