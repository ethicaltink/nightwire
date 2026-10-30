"""The export command lines. Pure functions, no subprocesses.

Two paths, and the difference matters to the person cutting the clip:

- **Copy** (`-c copy`): no re-encoding, so a 10-minute clip finishes in
  seconds. Input seeking lands on the keyframe at or before the mark, so the
  clip may start up to one GOP early. That is the trade-off the design notes names.
- **Precise** (`libx264 -preset veryfast`): ffmpeg's input seeking is
  frame-accurate when transcoding, because it decodes and discards up to the
  mark. Costs real CPU, which is why exports run one at a time under `nice`.

`-movflags +faststart` on both, so the MP4 plays while still downloading and
works on a phone.
"""

from __future__ import annotations

from pathlib import Path

FFMPEG = "ffmpeg"

# a precise-cut re-encode must never starve the
# recorders, so exports run at low priority.
NICE = "nice"
NICE_LEVEL = "10"

PRECISE_PRESET = "veryfast"


def export_argv(
    *,
    concat_list: Path,
    output: Path,
    seek_offset_ms: int,
    duration_ms: int,
    precise: bool = False,
) -> list[str]:
    """One export command.

    `-ss` goes **before** `-i`: input seeking. With `-c copy`, output seeking
    would drop packets before the offset and can leave the clip starting on a
    non-keyframe, which plays as a broken first second.
    """
    argv = [FFMPEG, "-hide_banner", "-loglevel", "warning", "-y"]
    # Only seek when there is something to seek past. ffmpeg refuses
    # `-ss 0.000` on the concat demuxer with "could not seek to position
    # 0.000", and a clip starting exactly on a segment boundary - which
    # "Clip this event" produces routinely - has a zero offset.
    if seek_offset_ms > 0:
        argv += ["-ss", f"{seek_offset_ms / 1000:.3f}"]
    argv += [
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list),
        "-t",
        f"{duration_ms / 1000:.3f}",
    ]
    if precise:
        argv += [
            "-c:v",
            "libx264",
            "-preset",
            PRECISE_PRESET,
            "-c:a",
            "aac",
        ]
    else:
        argv += ["-c", "copy"]
    argv += ["-movflags", "+faststart", str(output)]
    return argv


def niced(argv: list[str], level: str = NICE_LEVEL) -> list[str]:
    """Wrap a command in `nice`, so an export never starves a recorder."""
    return [NICE, "-n", level, *argv]
