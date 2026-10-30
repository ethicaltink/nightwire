"""Motion event endpoints, including the live SSE stream."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from .. import cameras as repo
from ..events import store
from ..events import topics as topics_mod
from ..events.bus import bus
from ..onvif import client as onvif
from ..playback import timeline as timeline_mod
from ..recording import paths

log = logging.getLogger("nightwire.api.events")
router = APIRouter()

SSE_KEEPALIVE_SECONDS = 20


class SourceEntry(BaseModel):
    topic: str
    source_filter: str | None = None
    label: str
    color: str | None = None
    kind: str


class SourcesBody(BaseModel):
    sources: list[SourceEntry] = []


def _error(message: str, status: int, **extra) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


def _events_supervisor(request: Request):
    return getattr(request.app.state, "events", None)


@router.get("/cameras/{camera_id}/event-topics")
async def event_topics(camera_id: str, request: Request) -> JSONResponse:
    """Ask the camera what it can emit. The "Rescan events" button.

    Nothing here knows a vendor's topic string: the camera's own
    `GetEventProperties` tree is flattened and `IsProperty` decides whether a
    topic becomes a span or a point.
    """
    conn = request.app.state.db
    secret = request.app.state.settings.secret_key
    try:
        row = repo.get_row(conn, camera_id)
        username, password = repo.credentials(conn, camera_id, secret)
        topic_set = await onvif.event_properties(
            row["host"], row["onvif_port"], username, password
        )
    except repo.NotFound:
        return _error(f"No camera with id {camera_id!r}.", 404)
    except onvif.OnvifUnavailable as exc:
        return _error(str(exc), 503, dependency_missing=True)
    except onvif.OnvifError as exc:
        return _error(str(exc), 502)

    found = topics_mod.flatten(topic_set)
    chosen = {
        topics_mod.source_key(source["topic"], source["source_filter"])
        for source in store.list_sources(conn, camera_id)
    }
    payload = []
    for topic in found:
        entry = topic.as_dict()
        entry["already_chosen"] = topics_mod.source_key(topic.path, "") in chosen
        payload.append(entry)
    return JSONResponse({"camera_id": camera_id, "topics": payload, "count": len(payload)})


@router.get("/cameras/{camera_id}/event-sources")
def get_sources(camera_id: str, request: Request) -> JSONResponse:
    conn = request.app.state.db
    try:
        repo.get_row(conn, camera_id)
    except repo.NotFound:
        return _error(f"No camera with id {camera_id!r}.", 404)
    supervisor = _events_supervisor(request)
    return JSONResponse(
        {
            "camera_id": camera_id,
            "sources": store.list_sources(conn, camera_id),
            "listener": supervisor.status(camera_id)
            if supervisor is not None
            else {"state": "not-started", "detail": "the event listener is not running"},
        }
    )


@router.put("/cameras/{camera_id}/event-sources")
async def put_sources(camera_id: str, body: SourcesBody, request: Request) -> JSONResponse:
    """Save the ticked sources. Unticked ones with events are disabled, not deleted."""
    conn = request.app.state.db
    try:
        repo.get_row(conn, camera_id)
        sources = store.replace_sources(
            conn, camera_id, [entry.model_dump() for entry in body.sources]
        )
    except repo.NotFound:
        return _error(f"No camera with id {camera_id!r}.", 404)
    except store.EventError as exc:
        return _error(str(exc), 400)

    supervisor = _events_supervisor(request)
    if supervisor is not None:
        await supervisor.resubscribe(camera_id)
    return JSONResponse({"camera_id": camera_id, "sources": sources})


@router.get("/cameras/{camera_id}/events")
def camera_events(
    camera_id: str,
    request: Request,
    date: str | None = None,
    sources: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> JSONResponse:
    """A day's events, for the timeline lanes and the Events panel."""
    conn = request.app.state.db
    tz = request.app.state.settings.tzinfo()
    try:
        repo.get_row(conn, camera_id)
    except repo.NotFound:
        return _error(f"No camera with id {camera_id!r}.", 404)

    if start is not None and end is not None:
        if end <= start:
            return _error("end must be after start", 400)
        start_ms, end_ms = int(start), int(end)
    else:
        from datetime import datetime

        day = date or datetime.now(tz).strftime("%Y-%m-%d")
        try:
            start_ms, end_ms = timeline_mod.day_bounds(day, tz)
        except ValueError:
            return _error(f"date must be YYYY-MM-DD, got {day!r}", 400)

    source_ids = None
    if sources:
        try:
            source_ids = [int(part) for part in sources.split(",") if part.strip()]
        except ValueError:
            return _error("sources must be a comma-separated list of ids", 400)

    rows = store.events_for_day(conn, camera_id, start_ms, end_ms, source_ids=source_ids)
    return JSONResponse(
        {
            "camera_id": camera_id,
            "start_ts": start_ms,
            "end_ts": end_ms,
            "events": rows,
            "sources": store.list_sources(conn, camera_id),
        }
    )


@router.get("/events/recent")
def recent_events(request: Request, limit: int = 10) -> JSONResponse:
    """Latest events across every camera. The Feeds strip."""
    return JSONResponse({"events": store.recent(request.app.state.db, limit)})


@router.get("/events/open")
def open_events(request: Request) -> JSONResponse:
    """Events currently open, so a page reload gets the glow right away."""
    return JSONResponse({"events": store.open_now(request.app.state.db)})


@router.get("/events/{event_id}/snapshot.jpg")
def event_snapshot(event_id: int, request: Request):
    conn = request.app.state.db
    settings = request.app.state.settings
    event = store.get_event(conn, event_id)
    if event is None:
        return _error(f"No event {event_id}.", 404)
    if not event.get("snapshot"):
        return _error("That event has no snapshot.", 404)
    path = paths.absolute_path(settings.recordings_dir, event["snapshot"])
    root = settings.recordings_dir.resolve()
    if root not in path.resolve().parents or not path.is_file():
        return _error("That event's snapshot is missing.", 404)
    return FileResponse(
        path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"}
    )


@router.get("/events/stream")
async def event_stream(request: Request) -> StreamingResponse:
    """Server-sent events for the live motion glow.

    One queue per browser, bounded, so a tab that stops reading loses
    notifications instead of growing the server's memory. The glow is a
    nicety; the database is the record.
    """
    queue = bus.subscribe()

    async def generator():
        # Tell a fresh subscriber what is already open, so a page reload does
        # not wait for the next transition to light up a tile.
        try:
            for event in store.open_now(request.app.state.db):
                yield _sse({"type": "open", "camera_id": event["camera_id"], "event": event})
            while True:
                if await request.is_disconnected():
                    return
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    # A comment keeps proxies and browsers from timing out.
                    yield ": keepalive\n\n"
                    continue
                yield _sse(message)
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _sse(message: dict) -> str:
    return f"data: {json.dumps(message)}\n\n"
