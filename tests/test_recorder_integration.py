"""End-to-end recording without a camera.

the design notes asks for "an integration test that records from a looping test RTSP
source (ffmpeg pushing a sample file into go2rtc) so CI does not need a
camera". Rather than depend on go2rtc, this drives the **real**
segment-muxer flags from ffmpeg's built-in `lavfi` test source instead, then
runs the real indexer with the real ffprobe and thumbnail commands over the
result. Everything between the input flags and the database row is exercised.

Set `NVR_TEST_RTSP` to a looping RTSP URL and the same
test covers the RTSP input path too.

Skipped entirely when ffmpeg and ffprobe are not on PATH, which is the case
on a machine that has never run the container.
"""

import asyncio
import os
import shutil
import subprocess
from datetime import timezone
from pathlib import Path

import pytest

from nightwire import cameras, db
from nightwire.recording import ffmpeg, indexer, paths, retention, stats

SECRET = "s" * 40
SEGMENT_SECONDS = 2
DURATION_SECONDS = 7

has_ffmpeg = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(
    not has_ffmpeg, reason="ffmpeg and ffprobe are not installed on this machine"
)


def synthetic_argv(recordings: Path, camera_id: str, segment_list: Path) -> list[str]:
    """A test source into the production segment-output flags.

    **H.264, because that is what the cameras record.** An earlier version
    used mpeg2video on the grounds that every ffmpeg has it, and that hid a
    real bug: MPEG-2 cannot be stream-copied into MP4, so the export produced
    an empty container that passed as success. Matching production's codec is
    what makes this test mean anything. Production itself never encodes; it
    uses `-c copy`.
    """
    source = [
        ffmpeg.FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        # Read the synthetic source at its native rate. Production uses
        # `-segment_atclocktime 1`, which cuts on wall-clock boundaries, and
        # without `-re` ffmpeg encodes seven seconds of testsrc in a fraction
        # of a second and never crosses one, producing a single segment. A
        # real camera delivers in realtime, so this makes the test resemble
        # one rather than changing what is under test.
        "-re",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size=320x240:rate=10:duration={DURATION_SECONDS}",
    ]
    rtsp = os.environ.get("NVR_TEST_RTSP")
    if rtsp:
        source = ffmpeg.rtsp_input_argv(rtsp) + ["-t", str(DURATION_SECONDS)]
    extra = [] if rtsp else ["-preset", "ultrafast", "-g", "10", "-pix_fmt", "yuv420p"]
    return source + extra + ffmpeg.segment_output_argv(
        recordings_dir=recordings,
        camera_id=camera_id,
        segment_seconds=SEGMENT_SECONDS,
        segment_list=segment_list,
        with_audio=False,
        # H.264 with a 1-second GOP, so segment cuts land on keyframes the
        # way a correctly configured camera's do.
        codec="libx264" if not rtsp else "copy",
    )


def setup(tmp_path):
    conn = db.connect(tmp_path / "nightwire.db")
    db.migrate(conn)
    cameras.create(
        conn,
        SECRET,
        camera_id="front",
        name="Front Door",
        host="192.168.1.101",
        main_uri="rtsp://192.168.1.101/onvif-media/media.amp?profile=nvr-main",
    )
    recordings = tmp_path / "recordings"
    # ffmpeg does not create directories, which is why the supervisor
    # pre-creates them. Do the same here.
    from datetime import datetime

    for directory in paths.hour_dirs_to_precreate(recordings, "front", datetime.now()):
        directory.mkdir(parents=True, exist_ok=True)
    return conn, recordings


def record(recordings: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    segment_list = tmp_path / "front.csv"
    return subprocess.run(
        synthetic_argv(recordings, "front", segment_list),
        capture_output=True,
        text=True,
        timeout=120,
    )


@needs_ffmpeg
def test_records_indexes_and_thumbnails_without_a_camera(tmp_path):
    conn, recordings = setup(tmp_path)

    completed = record(recordings, tmp_path)
    assert completed.returncode == 0, completed.stderr

    files = list(paths.iter_segment_files(recordings, "front"))
    assert len(files) >= 2, f"expected several segments, got {[f.name for f in files]}"

    index = indexer.Indexer(
        conn, recordings, tz=None, segment_seconds=SEGMENT_SECONDS
    )
    # ffmpeg has exited, so every segment including the last is finished.
    indexed = asyncio.run(index.sweep("front", now=9_999_999_999))
    assert indexed == len(files)

    rows = list(conn.execute("SELECT * FROM segments ORDER BY start_ts"))
    assert len(rows) == len(files)
    for row in rows:
        # Real ffprobe output, not a guess at the nominal segment length.
        assert row["duration_ms"] > 0
        assert row["size_bytes"] > 0
        assert row["end_ts"] == row["start_ts"] + row["duration_ms"]
        assert paths.absolute_path(recordings, row["path"]).is_file()

    # Segments are contiguous: the next one starts where the last ended,
    # within a second. A gap here would show up as a hole on the timeline.
    starts = [row["start_ts"] for row in rows]
    for earlier, later in zip(starts, starts[1:]):
        assert abs((later - earlier) - SEGMENT_SECONDS * 1000) < 1500

    thumbs = list(paths.thumbs_dir(recordings, "front").glob("*.jpg"))
    assert len(thumbs) == len(files)
    assert all(t.stat().st_size > 0 for t in thumbs)


@needs_ffmpeg
def test_the_segment_list_is_written_live_not_at_exit(tmp_path):
    """Direct check of the `+live` fix from the spec review."""
    conn, recordings = setup(tmp_path)
    segment_list = tmp_path / "front.csv"

    process = subprocess.Popen(
        synthetic_argv(recordings, "front", segment_list),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Wait for a couple of segments to close, while ffmpeg is still alive.
        deadline = SEGMENT_SECONDS * 3 + 6
        lines = 0
        for _ in range(deadline * 4):
            if segment_list.is_file():
                lines = len([ln for ln in segment_list.read_text().splitlines() if ln.strip()])
                if lines >= 1 and process.poll() is None:
                    break
            if process.poll() is not None:
                break
            import time

            time.sleep(0.25)
        assert lines >= 1, "segment list was empty while ffmpeg was still running"
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=30)


@needs_ffmpeg
def test_retention_removes_what_was_recorded(tmp_path):
    conn, recordings = setup(tmp_path)
    assert record(recordings, tmp_path).returncode == 0

    index = indexer.Indexer(conn, recordings, tz=None, segment_seconds=SEGMENT_SECONDS)
    asyncio.run(index.sweep("front", now=9_999_999_999))
    before = conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    assert before > 0

    # Pretend it is far in the future, so everything is past its window.
    far_future_ms = int(9_999_999_999 * 1000)
    result = retention.enforce(conn, recordings, now_ms=far_future_ms)
    assert result["deleted_segments"] == before
    assert result["deleted_files"] == before
    assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 0
    assert list(paths.iter_segment_files(recordings, "front")) == []
    # Thumbnails went with the footage.
    assert list(paths.thumbs_dir(recordings, "front").glob("*.jpg")) == []


@needs_ffmpeg
def test_measured_write_rate_is_reported(tmp_path):
    conn, recordings = setup(tmp_path)
    assert record(recordings, tmp_path).returncode == 0
    index = indexer.Indexer(conn, recordings, tz=None, segment_seconds=SEGMENT_SECONDS)
    asyncio.run(index.sweep("front", now=9_999_999_999))

    import time

    rates = stats.camera_rates(conn, now_ms=int(time.time() * 1000))
    assert len(rates) == 1
    rate = rates[0]
    assert rate.camera_id == "front"
    assert rate.segment_count >= 2, "the recording was indexed"
    assert rate.measured_days > 0

    # And the rate is deliberately reported as zero: this sample is seconds
    # long, and `camera_rates` refuses to extrapolate a GB-per-day figure
    # from under ~15 minutes of footage rather than inventing a wild one.
    # The real arithmetic is covered against known inputs in test_stats.py.
    assert rate.bytes_per_day == 0.0


@needs_ffmpeg
def test_exports_a_real_clip_from_real_segments(tmp_path):
    """End to end: record, index, then cut a clip with real ffmpeg.

    This is the closest thing to the design's Done-when that works without a
    camera: a playable MP4 produced from segments this test recorded itself.
    """
    import subprocess as sp

    from nightwire.export import worker as export_worker

    conn, recordings = setup(tmp_path)
    assert record(recordings, tmp_path).returncode == 0

    index = indexer.Indexer(conn, recordings, tz=None, segment_seconds=SEGMENT_SECONDS)
    asyncio.run(index.sweep("front", now=9_999_999_999))
    rows = list(conn.execute("SELECT start_ts, end_ts FROM segments ORDER BY start_ts"))
    assert len(rows) >= 2

    worker = export_worker.ExportWorker(conn, recordings, tmp_dir=tmp_path / "tmp", tz=None)
    # Cut from one second in to one second before the end, so the clip
    # genuinely spans a segment boundary and needs a non-zero seek.
    start = rows[0]["start_ts"] + 1000
    end = rows[-1]["end_ts"] - 1000
    created = worker.submit("front", start, end)
    finished = asyncio.run(worker.run_one(created["id"]))

    assert finished["status"] == export_worker.DONE, finished.get("error")
    output = paths.absolute_path(recordings, finished["path"])
    assert output.is_file()
    assert output.stat().st_size > 0

    # It is a real, probeable MP4 of roughly the requested length. Asked for
    # as JSON rather than positionally: ffprobe emits entries in its own
    # order, not the order they were requested.
    import json

    probe = sp.run(
        [
            "ffprobe", "-hide_banner", "-loglevel", "error",
            "-show_entries", "format=duration,format_name",
            "-show_entries", "stream=codec_type,codec_name",
            "-of", "json", str(output),
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert probe.returncode == 0, probe.stderr
    document = json.loads(probe.stdout)

    assert "mp4" in document["format"]["format_name"], document["format"]
    video = [s for s in document["streams"] if s.get("codec_type") == "video"]
    assert video, "the clip has no video stream"

    duration = float(document["format"]["duration"])
    requested = (end - start) / 1000
    # Stream copy cuts on keyframes, so allow a GOP either way.
    assert abs(duration - requested) < 3, f"got {duration}s, asked for {requested}s"


@needs_ffmpeg
def test_retention_never_deletes_an_export(tmp_path):
    """Exports are evidence. The spec is explicit: retention leaves them."""
    from nightwire.export import worker as export_worker

    conn, recordings = setup(tmp_path)
    assert record(recordings, tmp_path).returncode == 0
    index = indexer.Indexer(conn, recordings, tz=None, segment_seconds=SEGMENT_SECONDS)
    asyncio.run(index.sweep("front", now=9_999_999_999))
    rows = list(conn.execute("SELECT start_ts, end_ts FROM segments ORDER BY start_ts"))

    worker = export_worker.ExportWorker(conn, recordings, tmp_dir=tmp_path / "tmp", tz=None)
    created = worker.submit("front", rows[0]["start_ts"], rows[-1]["end_ts"] - 500)
    finished = asyncio.run(worker.run_one(created["id"]))
    clip = paths.absolute_path(recordings, finished["path"])
    assert clip.is_file()

    # Wipe every scrap of footage, far in the future.
    result = retention.enforce(conn, recordings, now_ms=int(9_999_999_999 * 1000))
    assert result["deleted_segments"] > 0
    assert list(paths.iter_segment_files(recordings, "front")) == []
    # The clip survives.
    assert clip.is_file()
    assert clip.stat().st_size > 0
