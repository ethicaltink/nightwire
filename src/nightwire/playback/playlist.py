"""HLS VOD playlist generation. Pure, standard library only.

A dynamically generated playlist over the recorded TS segments is what lets
the browser seek anywhere in a time range without the server re-encoding
anything. hls.js reads it, and `#EXT-X-PROGRAM-DATE-TIME` is what maps the
player's position back to wall-clock time for the playhead.

The details that matter, and why:

- `#EXT-X-ENDLIST` is required for a VOD playlist. Without it players treat
  the playlist as live and refuse to seek to the end.
- `#EXT-X-DISCONTINUITY` before a segment that does not follow the previous
  one, so the player resets its timestamp expectations and jumps the hole
  cleanly instead of stalling.
- `#EXTINF` carries the **measured** duration from ffprobe, never the nominal
  segment length. A guessed duration desynchronises the playhead silently.
- `#EXT-X-TARGETDURATION` must be at least the longest segment, rounded up,
  or strict players reject the playlist.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .timeline import GAP_TOLERANCE_MS, Segment, overlapping

HLS_VERSION = 3
MEDIA_PREFIX = "/media"


def program_date_time(start_ms: int) -> str:
    """ISO-8601 UTC with milliseconds, which is what HLS expects."""
    moment = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def segment_uri(relative_path: str, prefix: str = MEDIA_PREFIX) -> str:
    """`front/2026/.../x.ts` -> `/media/front/2026/.../x.ts`."""
    return f"{prefix.rstrip('/')}/{relative_path.lstrip('/')}"


def target_duration(segments: list[Segment]) -> int:
    """Longest segment in whole seconds, rounded up, minimum 1."""
    longest = 0
    for segment in segments:
        duration = segment.effective_end - segment.start_ts
        longest = max(longest, duration)
    seconds = -(-longest // 1000)  # ceiling division
    return max(1, int(seconds))


def build(
    segments: list[Segment],
    start_ms: int,
    end_ms: int,
    *,
    tolerance_ms: int = GAP_TOLERANCE_MS,
    prefix: str = MEDIA_PREFIX,
) -> str:
    """Render a VOD playlist for the segments overlapping a window.

    Returns a valid, empty-but-complete playlist when there is no footage, so
    the player reports "nothing here" rather than failing to parse.
    """
    window = overlapping(segments, start_ms, end_ms)

    lines = [
        "#EXTM3U",
        f"#EXT-X-VERSION:{HLS_VERSION}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        f"#EXT-X-TARGETDURATION:{target_duration(window)}",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]

    previous_end: int | None = None
    for segment in window:
        if previous_end is not None and segment.start_ts - previous_end > tolerance_ms:
            lines.append("#EXT-X-DISCONTINUITY")
        duration_ms = segment.effective_end - segment.start_ts
        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{program_date_time(segment.start_ts)}")
        lines.append(f"#EXTINF:{duration_ms / 1000:.3f},")
        lines.append(segment_uri(segment.path, prefix))
        previous_end = segment.effective_end

    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def discontinuity_count(playlist_text: str) -> int:
    """How many holes a rendered playlist contains. Used by tests and Core."""
    return playlist_text.count("#EXT-X-DISCONTINUITY")
