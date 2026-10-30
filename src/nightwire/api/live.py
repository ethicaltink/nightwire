"""Live video endpoints: the signalling proxy and the stream list.

The browser only ever talks to Nightwire's own port. go2rtc's API stays bound
to localhost and this module proxies the one socket the player needs, so
there is no second port to open, no second origin to allow, and no way to
reach go2rtc from the LAN directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import JSONResponse

from .. import cameras as repo
from .. import security, sessions
from ..live import client as live_client
from ..live import config as live_config

log = logging.getLogger("nightwire.api.live")

# HTTP data endpoints, mounted under /api so the session gate covers them.
router = APIRouter()
# The signalling socket, mounted at the root because the design notes' API table
# names it `/live/ws`. It checks the session cookie itself, since the
# middleware only gates /api.
ws_router = APIRouter()

# A browser that loses the socket reconnects, so a short close is normal.
WS_CLOSE_POLICY = 1008
WS_CLOSE_UNAVAILABLE = 1011


@router.get("/live/streams")
def list_streams(request: Request) -> JSONResponse:
    """What each camera's tile should play, plus go2rtc's own state.

    The grid uses the substream where there is one and falls back to the main
    stream, so a camera with only one profile still appears live.
    """
    conn = request.app.state.db
    supervisor = getattr(request.app.state, "go2rtc", None)
    cameras = repo.list_cameras(conn)

    tiles = []
    for camera in cameras:
        tiles.append(
            {
                "camera_id": camera["id"],
                "name": camera["name"],
                "grid_stream": live_config.grid_stream_for(camera),
                "main_stream": live_config.stream_name(camera["id"], "main"),
                "has_sub": bool(camera.get("sub_uri")),
                "record": camera["record"],
            }
        )

    return JSONResponse(
        {
            "cameras": tiles,
            "go2rtc": supervisor.status() if supervisor is not None else {
                "state": "not-started",
                "detail": "live video is not running in this process",
                "streams": [],
                "restarts": 0,
                "stderr_tail": [],
            },
        }
    )


@router.post("/live/sync")
async def sync(request: Request) -> JSONResponse:
    """Re-register every stream with go2rtc. Useful after editing a camera."""
    supervisor = getattr(request.app.state, "go2rtc", None)
    if supervisor is None:
        return JSONResponse(
            {"error": "Live video is not running in this process."}, status_code=503
        )
    try:
        return JSONResponse(await supervisor.sync())
    except live_client.LiveUnavailable as exc:
        return JSONResponse({"error": str(exc), "dependency_missing": True}, status_code=503)
    except live_client.LiveError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


def _authorised(websocket: WebSocket) -> bool:
    """WebSocket handshakes carry cookies, so the same session gate applies.

    This socket sits outside /api, so the middleware does not cover it and
    the check has to happen here. Fails closed if the database handle is
    missing, exactly like the middleware.
    """
    settings = websocket.app.state.settings
    connection = getattr(websocket.app.state, "db", None)
    if connection is None:
        return False
    return (
        sessions.verify(
            connection,
            websocket.cookies.get(security.COOKIE_NAME),
            password_fp=security.password_fingerprint(settings.secret_key, settings.password),
            ttl_seconds=settings.session_days * 86400,
        )
        is not None
    )


@ws_router.websocket("/live/ws")
async def live_ws(websocket: WebSocket) -> None:
    """Proxy go2rtc's signalling socket for one stream.

    The player sets its source to `/live/ws?src=<stream>`; this forwards to
    go2rtc on localhost and pumps frames both ways until either side closes.
    """
    stream = websocket.query_params.get("src", "")
    if not _authorised(websocket):
        await websocket.close(code=WS_CLOSE_POLICY)
        return
    if not stream or live_config.parse_stream_name(stream) is None:
        await websocket.close(code=WS_CLOSE_POLICY)
        return

    try:
        import websockets
    except ModuleNotFoundError:
        log.error("live signalling needs the `websockets` package (ships with uvicorn[standard])")
        await websocket.close(code=WS_CLOSE_UNAVAILABLE)
        return

    await websocket.accept()
    upstream = None
    try:
        upstream = await websockets.connect(live_config.ws_url(stream), open_timeout=5)
    except Exception as exc:  # noqa: BLE001 - go2rtc down, or not yet ready
        log.warning("live: could not reach go2rtc for %s: %s", stream, exc)
        await websocket.close(code=WS_CLOSE_UNAVAILABLE)
        return

    async def browser_to_go2rtc() -> None:
        while True:
            message = await websocket.receive_text()
            await upstream.send(message)

    async def go2rtc_to_browser() -> None:
        async for message in upstream:
            if isinstance(message, bytes):
                await websocket.send_bytes(message)
            else:
                await websocket.send_text(message)

    pumps = [asyncio.create_task(browser_to_go2rtc()), asyncio.create_task(go2rtc_to_browser())]
    try:
        done, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    except Exception as exc:  # noqa: BLE001 - a dropped socket is routine
        log.debug("live socket for %s ended: %s", stream, exc)
    finally:
        with contextlib.suppress(Exception):
            await upstream.close()
        with contextlib.suppress(Exception):
            await websocket.close()
