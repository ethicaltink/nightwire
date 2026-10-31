"""Playback: the timeline, the VOD playlist, thumbnails and segment files.

`/media/...` is gated by the same session check as `/api` (see main.py), so
recorded footage is never reachable without signing in. The playlist, the
timeline and the thumbnails all read the index; none of them touch a camera.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from .. import cameras as repo
from ..playback import playlist as playlist_mod
from ..playback import timeline as timeline_mod
from ..recording import paths

log = logging.getLogger("nightwire.api.playback")
router = APIRouter()
media_router = APIRouter()

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RANGE_PATTERN = re.compile(r"^bytes=(\d*)-(\d*)$")
CHUNK = 64 * 1024


def _error(message: str, status: int, **extra) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


def _segments(conn, camera_id: str, start_ms: int, end_ms: int) -> list[timeline_mod.Segment]:
    """Segments overlapping a window, widened by a day on the low side.

    A segment that began before the window can still carry its first frames,
    and SQLite cannot express "overlaps" cheaply without an end index, so the
    query is widened and `overlapping` does the precise filtering.
    """
    rows = conn.execute(
        "SELECT id, start_ts, end_ts, duration_ms, path, size_bytes, has_audio"
        " FROM segments WHERE camera_id = ? AND start_ts >= ? AND start_ts < ?"
        " ORDER BY start_ts",
        (camera_id, start_ms - timeline_mod.DAY_MS, end_ms),
    )
    return [
        timeline_mod.Segment(
            id=row["id"],
            start_ts=row["start_ts"],
            end_ts=row["end_ts"],
            duration_ms=row["duration_ms"],
            path=row["path"],
            size_bytes=row["size_bytes"],
            has_audio=bool(row["has_audio"]),
        )
        for row in rows
    ]


def _window(request: Request, date: str | None, start: int | None, end: int | None):
    """Resolve the requested window: explicit milliseconds, or a local day."""
    tz = request.app.state.settings.tzinfo()
    if start is not None and end is not None:
        if end <= start:
            raise ValueError("end must be after start")
        return int(start), int(end)
    day = date or datetime.now(tz).strftime("%Y-%m-%d")
    if not DATE_PATTERN.match(day):
        raise ValueError(f"date must be YYYY-MM-DD, got {day!r}")
    return timeline_mod.day_bounds(day, tz)


@router.get("/cameras/{camera_id}/days")
def camera_days(camera_id: str, request: Request) -> JSONResponse:
    """Which local days have footage for this camera.

    The calendar on Rewind offers only these, so a date cannot be picked
    that lands on an empty page. Deliberately one query over the whole
    `segments` table for this camera rather than a window: the index on
    (camera_id, start_ts) makes it cheap, a 45-day archive is at most 45
    rows out, and the calendar wants to know about months it has not shown
    yet.
    """
    conn = request.app.state.db
    tz = request.app.state.settings.tzinfo()
    try:
        repo.get_row(conn, camera_id)
    except Exception as exc:  # noqa: BLE001
        return _error(str(exc), 404)

    rows = conn.execute(
        "SELECT id, start_ts, end_ts, duration_ms, path, size_bytes, has_audio"
        " FROM segments WHERE camera_id = ? ORDER BY start_ts",
        (camera_id,),
    )
    segments = [
        timeline_mod.Segment(
            id=row["id"],
            start_ts=row["start_ts"],
            end_ts=row["end_ts"],
            duration_ms=row["duration_ms"],
            path=row["path"],
            size_bytes=row["size_bytes"],
            has_audio=bool(row["has_audio"]),
        )
        for row in rows
    ]
    days = timeline_mod.days_with_footage(segments, tz)
    return JSONResponse(
        {
            "camera_id": camera_id,
            "days": days,
            "first": days[0]["date"] if days else None,
            "last": days[-1]["date"] if days else None,
            "today": datetime.now(tz).strftime("%Y-%m-%d"),
        }
    )


@router.get("/cameras/{camera_id}/timeline")
def camera_timeline(
    camera_id: str,
    request: Request,
    date: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> JSONResponse:
    """Merged recorded ranges, gaps and the not-yet-recorded tail for a day."""
    conn = request.app.state.db
    try:
        repo.get_row(conn, camera_id)
        start_ms, end_ms = _window(request, date, start, end)
    except repo.NotFound:
        return _error(f"No camera with id {camera_id!r}.", 404)
    except ValueError as exc:
        return _error(str(exc), 400)

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    payload = timeline_mod.build(
        _segments(conn, camera_id, start_ms, end_ms), start_ms, end_ms, now_ms=now_ms
    )
    payload["camera_id"] = camera_id
    return JSONResponse(payload)


@router.get("/cameras/{camera_id}/vod.m3u8")
def camera_vod(
    camera_id: str,
    request: Request,
    date: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> Response:
    """An HLS VOD playlist over the recorded segments in a window."""
    conn = request.app.state.db
    try:
        repo.get_row(conn, camera_id)
        start_ms, end_ms = _window(request, date, start, end)
    except repo.NotFound:
        return _error(f"No camera with id {camera_id!r}.", 404)
    except ValueError as exc:
        return _error(str(exc), 400)

    text = playlist_mod.build(_segments(conn, camera_id, start_ms, end_ms), start_ms, end_ms)
    return Response(
        content=text,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/cameras/{camera_id}/thumb")
def camera_thumb(camera_id: str, request: Request, t: int | None = None):
    """The thumbnail of the segment nearest a moment, for timeline hover."""
    conn = request.app.state.db
    settings = request.app.state.settings
    try:
        repo.get_row(conn, camera_id)
    except repo.NotFound:
        return _error(f"No camera with id {camera_id!r}.", 404)

    at_ms = int(t) if t is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    window = _segments(conn, camera_id, at_ms - timeline_mod.DAY_MS, at_ms + timeline_mod.DAY_MS)
    segment = timeline_mod.nearest_segment(window, at_ms)
    if segment is None:
        return _error("No footage near that time.", 404)

    absolute = paths.absolute_path(settings.recordings_dir, segment.path)
    thumb = paths.thumb_path(settings.recordings_dir, camera_id, absolute)
    if not thumb.is_file():
        return _error("No thumbnail for that segment.", 404)
    return FileResponse(
        thumb,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=3600", "X-Segment-Start": str(segment.start_ts)},
    )


@router.get("/cameras/{camera_id}/segment")
def camera_segment_info(camera_id: str, request: Request, t: int) -> JSONResponse:
    """What segment covers a moment, and where to download it.

    Behind the timeline's context-menu "download this segment", which the
    spec lists as a smaller extra.
    """
    conn = request.app.state.db
    try:
        repo.get_row(conn, camera_id)
    except repo.NotFound:
        return _error(f"No camera with id {camera_id!r}.", 404)

    at_ms = int(t)
    window = _segments(conn, camera_id, at_ms - timeline_mod.DAY_MS, at_ms + timeline_mod.DAY_MS)
    segment = timeline_mod.nearest_segment(window, at_ms)
    if segment is None:
        return _error("No footage near that time.", 404)
    return JSONResponse(
        {
            "camera_id": camera_id,
            "start_ts": segment.start_ts,
            "end_ts": segment.effective_end,
            "duration_ms": segment.duration_ms,
            "size_bytes": segment.size_bytes,
            "has_audio": segment.has_audio,
            "url": playlist_mod.segment_uri(segment.path),
            "contains": segment.start_ts <= at_ms < segment.effective_end,
        }
    )


def _safe_media_path(recordings_dir: Path, relative: str) -> Path | None:
    """Resolve a /media path, refusing anything outside the volume.

    Camera ids are already validated on the way in, but this is the one route
    that turns user input into a filesystem path, so it is checked here too.
    """
    root = Path(recordings_dir).resolve()
    candidate = (root / relative).resolve()
    if root != candidate and root not in candidate.parents:
        return None
    if not candidate.is_file():
        return None
    # Only recorded media is served here. Exports have their own route in
    # exports, and nothing else on the volume is downloadable.
    if candidate.suffix.lower() not in (".ts", ".jpg"):
        return None
    return candidate


@media_router.get("/media/{relative:path}")
def media(relative: str, request: Request):
    """A recorded segment, with HTTP range support so players can seek.

    hls.js usually fetches whole segments, but range support is what makes a
    raw segment download resumable and lets a plain <video> element seek.
    """
    settings = request.app.state.settings
    path = _safe_media_path(settings.recordings_dir, relative)
    if path is None:
        return _error("Not found.", 404)

    media_type = "video/mp2t" if path.suffix.lower() == ".ts" else "image/jpeg"
    size = path.stat().st_size
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(
            path,
            media_type=media_type,
            headers={"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"},
        )

    match = RANGE_PATTERN.match(range_header.strip())
    if not match:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    first_text, last_text = match.groups()
    if first_text:
        first = int(first_text)
        last = int(last_text) if last_text else size - 1
    elif last_text:  # suffix range: the last N bytes
        first = max(0, size - int(last_text))
        last = size - 1
    else:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

    last = min(last, size - 1)
    if first > last or first >= size:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

    length = last - first + 1

    def stream():
        with path.open("rb") as handle:
            handle.seek(first)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        stream(),
        status_code=206,
        media_type=media_type,
        headers={
            "Content-Range": f"bytes {first}-{last}/{size}",
            "Content-Length": str(length),
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=3600",
        },
    )
