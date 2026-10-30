"""Planning an export: which segments, what offset, how long. Pure.

An export is a cut out of the recorded segments, so the arithmetic is: find
the segments covering the requested window, concatenate them, seek into the
first one, and keep the requested duration. All of that is decidable without
a disk or ffmpeg, so all of it is tested.

**One deliberate departure from the obvious command.** the design notes shows
`-i list.txt -ss <offset>`, which is *output* seeking: with `-c copy` that
drops packets before the offset and can leave the clip starting on a
non-keyframe, which players show as a broken first second. The behaviour the
spec actually describes - "stream copy cuts on keyframes, so a clip may start
up to one GOP early" - is *input* seeking, `-ss <offset> -i list.txt`. So the
offset goes before the input. See `export/ffmpeg.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..playback.timeline import Segment, overlapping

# one export is limited to 2 hours by default.
MAX_DURATION_MS = 2 * 60 * 60 * 1000
MIN_DURATION_MS = 1000


class ExportError(ValueError):
    """The requested export cannot be made."""


@dataclass(frozen=True)
class ExportPlan:
    camera_id: str
    start_ts: int
    end_ts: int
    precise: bool
    segments: list[Segment]
    name: str

    @property
    def duration_ms(self) -> int:
        return self.end_ts - self.start_ts

    @property
    def seek_offset_ms(self) -> int:
        """How far into the concatenated segments the clip starts.

        The concat timeline begins at the first segment's start, which is at
        or before the requested start, so this is never negative.
        """
        return max(0, self.start_ts - self.segments[0].start_ts)

    @property
    def covered_ms(self) -> int:
        """How much of the requested window actually has footage."""
        total = 0
        for segment in self.segments:
            overlap_start = max(segment.start_ts, self.start_ts)
            overlap_end = min(segment.effective_end, self.end_ts)
            if overlap_end > overlap_start:
                total += overlap_end - overlap_start
        return total

    @property
    def has_gaps(self) -> bool:
        """Whether the window is missing footage.

        Not an error: a clip spanning the cable-pull gap is still worth
        exporting, and the Vault page says so rather than refusing.
        """
        return self.covered_ms < self.duration_ms - 1000

    def as_dict(self) -> dict:
        return {
            "camera_id": self.camera_id,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "duration_ms": self.duration_ms,
            "precise": self.precise,
            "segment_count": len(self.segments),
            "seek_offset_ms": self.seek_offset_ms,
            "covered_ms": self.covered_ms,
            "has_gaps": self.has_gaps,
            "name": self.name,
        }


def export_name(camera_id: str, start_ms: int, duration_ms: int, tz=None) -> str:
    """`<camera>_<YYYYMMDD-HHMMSS>_<duration>.mp4`, per the spec.

    The timestamp is local, because it is what the person reads off the
    timeline and then off their download folder.
    """
    moment = datetime.fromtimestamp(start_ms / 1000, tz or timezone.utc)
    stamp = moment.strftime("%Y%m%d-%H%M%S")
    total_seconds = max(0, round(duration_ms / 1000))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        length = f"{hours}h{minutes:02d}m"
    elif minutes:
        length = f"{minutes}m{seconds:02d}s"
    else:
        length = f"{seconds}s"
    return f"{camera_id}_{stamp}_{length}.mp4"


def build(
    camera_id: str,
    segments: list[Segment],
    start_ms: int,
    end_ms: int,
    *,
    precise: bool = False,
    max_duration_ms: int = MAX_DURATION_MS,
    tz=None,
) -> ExportPlan:
    """Validate a request and work out how to cut it."""
    if end_ms <= start_ms:
        raise ExportError("The clip's end must be after its start.")
    duration = end_ms - start_ms
    if duration < MIN_DURATION_MS:
        raise ExportError("A clip must be at least one second long.")
    if duration > max_duration_ms:
        hours = max_duration_ms / 3_600_000
        raise ExportError(
            f"That clip is {duration / 3_600_000:.1f} hours. One export is limited to "
            f"{hours:.0f} hours; mark a shorter range or make several clips."
        )

    covering = overlapping(segments, start_ms, end_ms)
    if not covering:
        raise ExportError("There is no recorded footage in that range.")

    return ExportPlan(
        camera_id=camera_id,
        start_ts=start_ms,
        end_ts=end_ms,
        precise=precise,
        segments=covering,
        name=export_name(camera_id, start_ms, duration, tz),
    )


def concat_list(plan: ExportPlan, recordings_dir) -> str:
    """The ffmpeg concat demuxer list.

    `-safe 0` is what allows absolute paths. A single quote in a path would
    break the quoting, so it is escaped the way the demuxer expects; camera
    ids cannot contain one, but the path is data and is treated as such.
    """
    from pathlib import Path

    lines = []
    for segment in plan.segments:
        absolute = Path(recordings_dir) / segment.path
        escaped = str(absolute).replace("'", r"'\''")
        lines.append(f"file '{escaped}'")
    return "\n".join(lines) + "\n"
