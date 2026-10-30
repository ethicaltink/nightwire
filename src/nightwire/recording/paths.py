"""Segment paths and filenames. Pure functions, standard library only.

The layout is fixed by /recordings/<camera_id>/YYYY/MM/DD/HH/YYYYMMDDTHHMMSS.ts
    /recordings/_thumbs/<camera_id>/YYYYMMDDTHHMMSS.jpg
    /recordings/_events/<camera_id>/<event_id>.jpg
    /recordings/_exports/<export_id>.mp4

ffmpeg will not create directories, so the supervisor pre-creates the hour
folder before it is needed. Timestamps in filenames are **local time**,
because `-strftime 1` uses the process clock; the database stores UTC
milliseconds. `TZ` is fixed for the container's life, so parsing back is
unambiguous except across a DST fold, which `segment_start_ms` handles by
trusting the file's own mtime when the two disagree wildly.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEGMENT_SUFFIX = ".ts"
THUMB_SUFFIX = ".jpg"
FILENAME_FORMAT = "%Y%m%dT%H%M%S"
FILENAME_PATTERN = re.compile(r"^(\d{8}T\d{6})\.ts$")

RESERVED_DIRS = ("_thumbs", "_events", "_exports")

# ffmpeg's -strftime pattern for the segment muxer, matching the layout above.
SEGMENT_TEMPLATE = "%Y/%m/%d/%H/%Y%m%dT%H%M%S.ts"


def camera_dir(recordings_dir: Path, camera_id: str) -> Path:
    return recordings_dir / camera_id


def segment_pattern(recordings_dir: Path, camera_id: str) -> str:
    """The -strftime output pattern handed to ffmpeg."""
    return str(camera_dir(recordings_dir, camera_id) / SEGMENT_TEMPLATE)


def thumbs_dir(recordings_dir: Path, camera_id: str) -> Path:
    return recordings_dir / "_thumbs" / camera_id


def events_dir(recordings_dir: Path, camera_id: str) -> Path:
    return recordings_dir / "_events" / camera_id


def exports_dir(recordings_dir: Path) -> Path:
    return recordings_dir / "_exports"


def hour_dir(recordings_dir: Path, camera_id: str, moment: datetime) -> Path:
    return camera_dir(recordings_dir, camera_id) / moment.strftime("%Y/%m/%d/%H")


def hour_dirs_to_precreate(
    recordings_dir: Path, camera_id: str, now: datetime, hours_ahead: int = 2
) -> list[Path]:
    """This hour and the next few, so ffmpeg never rolls into a missing folder."""
    return [
        hour_dir(recordings_dir, camera_id, now + timedelta(hours=offset))
        for offset in range(hours_ahead + 1)
    ]


def thumb_path(recordings_dir: Path, camera_id: str, segment_path: Path) -> Path:
    return thumbs_dir(recordings_dir, camera_id) / (segment_path.stem + THUMB_SUFFIX)


def is_segment(path: Path) -> bool:
    return bool(FILENAME_PATTERN.match(path.name))


def parse_filename(name: str) -> datetime | None:
    """Local-time datetime from a segment filename, or None if it is not one."""
    match = FILENAME_PATTERN.match(name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), FILENAME_FORMAT)
    except ValueError:
        return None


def relative_path(recordings_dir: Path, path: Path) -> str:
    """The `segments.path` value: always relative to the recordings volume."""
    try:
        return str(Path(path).resolve().relative_to(Path(recordings_dir).resolve()))
    except ValueError:
        # Not under the volume. Store what we were given rather than a
        # misleading absolute path; the reconcile pass will drop it.
        return str(path)


def absolute_path(recordings_dir: Path, relative: str) -> Path:
    return Path(recordings_dir) / relative


def camera_id_from_relative(relative: str) -> str | None:
    """'front/2026/10/27/14/20261027T143200.ts' -> 'front'."""
    parts = Path(relative).parts
    if not parts or parts[0] in RESERVED_DIRS:
        return None
    return parts[0]


def segment_start_ms(path: Path, tz: timezone | None, mtime: float | None = None) -> int:
    """Wall-clock start of a segment, as unix milliseconds UTC.

    The filename is authoritative, interpreted in `tz`. If the file's mtime
    disagrees by more than a day the clock or time zone changed under us, so
    the mtime wins: a segment's timestamp being an hour out is survivable, a
    year out is not.
    """
    naive = parse_filename(path.name)
    if naive is None:
        if mtime is None:
            raise ValueError(f"{path.name} is not a segment filename")
        return int(mtime * 1000)

    aware = naive.replace(tzinfo=tz) if tz is not None else naive.replace(tzinfo=timezone.utc)
    from_name = int(aware.timestamp() * 1000)
    if mtime is not None and abs(int(mtime * 1000) - from_name) > 86_400_000:
        return int(mtime * 1000)
    return from_name


def iter_segment_files(recordings_dir: Path, camera_id: str):
    """Every segment file for a camera, in chronological (filename) order."""
    root = camera_dir(recordings_dir, camera_id)
    if not root.is_dir():
        return []
    return sorted((p for p in root.rglob("*" + SEGMENT_SUFFIX) if is_segment(p)), key=lambda p: p.name)


def prune_empty_dirs(root: Path, stop_at: Path) -> int:
    """Remove empty hour/day/month/year folders, walking up from `root`.

    Called after retention deletes files. Stops at `stop_at` and never
    removes it, so the camera folder and the volume itself survive.
    """
    removed = 0
    current = Path(root)
    stop = Path(stop_at).resolve()
    while True:
        resolved = current.resolve()
        if resolved == stop or stop not in resolved.parents:
            break
        if not current.is_dir():
            current = current.parent
            continue
        if any(current.iterdir()):
            break
        current.rmdir()
        removed += 1
        current = current.parent
    return removed
