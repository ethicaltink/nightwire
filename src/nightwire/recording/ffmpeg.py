"""The ffmpeg and ffprobe command lines. Pure functions, no subprocesses.

Keeping argv construction separate from process management means the exact
flags are unit-testable, which matters: the difference between
`-segment_list_flags +live` and the default `+cache` is the difference
between an indexer that sees every segment and one that sees none.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import paths

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

# Socket read timeout for the RTSP demuxer, in microseconds. Covers a dead
# connection; a connection that stays open but stops delivering frames is the
# stall watchdog's job.
RTSP_TIMEOUT_US = 5_000_000

THUMB_WIDTH = 320


def rtsp_input_argv(rtsp_uri: str) -> list[str]:
    """The input half: pull one camera's RTSP stream over TCP."""
    return [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-rtsp_transport",
        "tcp",
        "-timeout",
        str(RTSP_TIMEOUT_US),
        "-i",
        rtsp_uri,
    ]


def segment_output_argv(
    *,
    recordings_dir: Path,
    camera_id: str,
    segment_seconds: int = 60,
    segment_list: Path | None = None,
    with_audio: bool = True,
    codec: str = "copy",
) -> list[str]:
    """The output half: fixed-length MPEG-TS segments, named by wall clock.

    Separated from the input so the integration test can drive the real
    segment-muxer flags from a synthetic source, with `codec` set to an
    encoder instead of `copy`. Production always uses `copy`.
    """
    argv = ["-map", "0:v"]
    if with_audio:
        # The trailing ? makes the audio stream optional, so a camera with
        # audio disabled still records video instead of failing to start.
        argv += ["-map", "0:a?"]
    argv += [
        "-c",
        codec,
        "-f",
        "segment",
        "-segment_time",
        str(segment_seconds),
        "-segment_atclocktime",
        "1",
        "-reset_timestamps",
        "1",
        "-strftime",
        "1",
    ]
    if segment_list is not None:
        argv += [
            "-segment_list",
            str(segment_list),
            "-segment_list_type",
            "csv",
            # Without +live the muxer buffers the list and only writes it when
            # ffmpeg exits, so a 24/7 recorder would never append a line. The
            # indexer does not rely on this file, but a stale one is worse
            # than a live one.
            "-segment_list_flags",
            "+live",
        ]
    argv.append(paths.segment_pattern(recordings_dir, camera_id))
    return argv


def record_argv(
    *,
    rtsp_uri: str,
    recordings_dir: Path,
    camera_id: str,
    segment_seconds: int = 60,
    segment_list: Path | None = None,
    with_audio: bool = True,
) -> list[str]:
    """One camera's recording command.

    Stream copy, so near-zero CPU and no re-encoding. MPEG-TS segments,
    because a TS file is playable even if ffmpeg is killed mid-write and the
    segments drop straight into an HLS playlist.
    """
    return rtsp_input_argv(rtsp_uri) + segment_output_argv(
        recordings_dir=recordings_dir,
        camera_id=camera_id,
        segment_seconds=segment_seconds,
        segment_list=segment_list,
        with_audio=with_audio,
    )


def probe_argv(segment_path: Path) -> list[str]:
    """ffprobe command for a segment's exact duration and stream layout."""
    return [
        FFPROBE,
        "-hide_banner",
        "-loglevel",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type",
        "-of",
        "json",
        str(segment_path),
    ]


def parse_probe(stdout: str | bytes) -> dict:
    """Turn ffprobe JSON into {duration_ms, has_audio}.

    A segment whose duration cannot be read is reported as 0 rather than
    guessed at the nominal segment length: the indexer stores what ffprobe
    measured, and the playlist generator depends on it being real.
    """
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    try:
        document = json.loads(stdout or "{}")
    except ValueError:
        return {"duration_ms": 0, "has_audio": False}

    raw_duration = (document.get("format") or {}).get("duration")
    try:
        duration_ms = int(round(float(raw_duration) * 1000))
    except (TypeError, ValueError):
        duration_ms = 0

    streams = document.get("streams") or []
    has_audio = any((s or {}).get("codec_type") == "audio" for s in streams)
    return {"duration_ms": max(0, duration_ms), "has_audio": has_audio}


def thumbnail_argv(segment_path: Path, thumb_path: Path, width: int = THUMB_WIDTH) -> list[str]:
    """One keyframe, scaled. `-skip_frame nokey` keeps this nearly free."""
    return [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-skip_frame",
        "nokey",
        "-i",
        str(segment_path),
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:-1",
        str(thumb_path),
    ]
