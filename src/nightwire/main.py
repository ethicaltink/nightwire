"""Application factory and request gating.

Everything is served from one port: the API under /api, the built frontend
as static files, and the single-page shell for every other path.

What is public and what is not: /api/health, the login and session
endpoints, and the frontend's own assets are reachable without a session,
because the sign-in page has to load before anyone can sign in. Every other
/api route requires the session cookie, and no camera, footage, export or
settings data is reachable without one.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, health, logs, recorders, security, seed, sessions
from .recording.supervisor import Supervisor
from .api import auth as auth_api
from .api import cameras as cameras_api
from .api import health as health_api
from .api import live as live_api
from .api import events as events_api
from .api import exports as exports_api
from .export.worker import ExportWorker
from .api import playback as playback_api
from .api import system as system_api
from .events.listener import EventSupervisor
from .live.process import Go2rtcSupervisor
from .ratelimit import LoginLimiter

log = logging.getLogger("nightwire")

PUBLIC_API_PATHS = frozenset(
    {
        "/api/health",
        "/metrics",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/session",
    }
)

# The one deliberate exception to "colour lives only in tokens.css": this
# page is served when the Svelte build is absent, so it cannot import the
# stylesheet it would need. Values are copied from the Look table.
_NO_FRONTEND = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Nightwire</title>
<style>body{background:#0A0612;color:#EDE9F7;font:15px/1.6 system-ui,sans-serif;margin:0;
display:flex;align-items:center;justify-content:center;min-height:100vh}div{max-width:38rem;padding:2rem}
code{color:#00F0FF}</style></head><body><div>
<h1>Frontend not built</h1>
<p>The API is running, but no static build was found at <code>__STATIC_DIR__</code>.</p>
<p>Build it with <code>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</code>,
or run the container image, which builds it at image build time.</p>
<p><code>/api/health</code> works regardless.</p>
</div></body></html>
"""


def _configure_logging() -> None:
    """Human-readable by default, JSON with NVR_LOG_FORMAT=json."""
    chosen = logs.configure()
    log.debug("logging configured in %s format", chosen)


def create_app(settings: config.Settings | None = None) -> FastAPI:
    _configure_logging()
    resolved = config.load() if settings is None else settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        connection = db.connect(resolved.db_path)
        applied = db.migrate(connection)
        if applied:
            log.info("applied migrations: %s", ", ".join(applied))
        app.state.db = connection
        # A password or secret-key change invalidates sessions issued before
        # it. Done here as well as per request so it takes effect for a
        # browser that never makes another request, and so the table does
        # not keep rows that can never be valid again.
        sessions.revoke_stale_fingerprints(
            connection,
            security.password_fingerprint(resolved.secret_key, resolved.password),
        )
        sessions.prune(connection)
        # Optional declarative seeding. Never fatal: a camera that is down, or
        # a malformed file, must not stop the app from starting.
        app.state.seed_result = seed.seed_from_config_dir(
            connection, resolved.secret_key, resolved.config_dir
        )

        go2rtc = None
        if os.environ.get("NVR_DISABLE_LIVE") == "1":
            log.warning("NVR_DISABLE_LIVE=1: not starting go2rtc")
        else:
            go2rtc = Go2rtcSupervisor(
                connection, resolved.config_dir, secret_key=resolved.secret_key
            )
            await go2rtc.start()
            health.register("go2rtc", go2rtc.health)
        app.state.go2rtc = go2rtc

        exports = None
        if os.environ.get("NVR_DISABLE_EXPORTS") == "1":
            log.warning("NVR_DISABLE_EXPORTS=1: not starting the export worker")
        else:
            exports = ExportWorker(
                connection,
                resolved.recordings_dir,
                tmp_dir=resolved.tmp_dir,
                tz=resolved.tzinfo(),
            )
            await exports.start()
        app.state.exports = exports

        events = None
        if os.environ.get("NVR_DISABLE_EVENTS") == "1":
            log.warning("NVR_DISABLE_EVENTS=1: not starting the event listener")
        else:
            events = EventSupervisor(
                connection,
                resolved.recordings_dir,
                secret_key=resolved.secret_key,
            )
            await events.start()
            health.register("events", events.health)
        app.state.events = events

        supervisor = None
        if os.environ.get("NVR_DISABLE_RECORDER") == "1":
            log.warning("NVR_DISABLE_RECORDER=1: not starting the recorder")
        else:
            segment_seconds = int(db.get_setting(connection, "segment_seconds", "60") or 60)
            supervisor = Supervisor(
                connection,
                resolved.recordings_dir,
                secret_key=resolved.secret_key,
                tz=resolved.tzinfo(),
                tmp_dir=resolved.tmp_dir,
                segment_seconds=segment_seconds,
            )
            await supervisor.start()
            # Both the Nodes page and /api/health read recorder state through
            # these registries, so neither has to know the supervisor exists.
            recorders.register(supervisor)
            health.register("recorders", supervisor.health)
        app.state.supervisor = supervisor

        log.info("nightwire ready on port %s, tz %s", resolved.port, resolved.timezone)
        try:
            yield
        finally:
            # Ordered: stop ffmpeg and index the last segment before the
            # database connection goes away.
            if supervisor is not None:
                await supervisor.stop()
                recorders.register(None)
            if exports is not None:
                await exports.stop()
            if events is not None:
                await events.stop()
            if go2rtc is not None:
                await go2rtc.stop()
            connection.close()

    app = FastAPI(
        title="Nightwire",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = resolved
    app.state.login_limiter = LoginLimiter()

    @app.middleware("http")
    async def require_session(request: Request, call_next):
        path = request.url.path
        # /media carries recorded footage, so it is gated exactly like /api.
        # The spec puts playlist segment URIs under /media, and leaving that
        # open would publish 45 days of video to anyone on the LAN.
        if (path.startswith("/api") or path.startswith("/media")) and path not in PUBLIC_API_PATHS:
            # Fails closed: no database handle means no way to check a
            # session, which is a 401 and not an open door.
            connection = getattr(request.app.state, "db", None)
            row = (
                None
                if connection is None
                else sessions.verify(
                    connection,
                    request.cookies.get(security.COOKIE_NAME),
                    password_fp=security.password_fingerprint(
                        resolved.secret_key, resolved.password
                    ),
                    ttl_seconds=resolved.session_days * 86400,
                )
            )
            if row is None:
                return JSONResponse({"error": "Not signed in."}, status_code=401)
        return await call_next(request)

    app.include_router(health_api.router, prefix="/api")
    app.include_router(auth_api.router, prefix="/api")
    app.include_router(cameras_api.router, prefix="/api")
    app.include_router(live_api.router, prefix="/api")
    app.include_router(playback_api.router, prefix="/api")
    app.include_router(events_api.router, prefix="/api")
    app.include_router(exports_api.router, prefix="/api")
    app.include_router(system_api.router, prefix="/api")
    # /metrics sits at the root for Prometheus, and is reachable without a
    # session because a scraper cannot hold a cookie. It exposes camera ids,
    # bitrates and disk usage, never footage. NVR_METRICS_TOKEN closes it.
    app.include_router(system_api.metrics_router)
    # /media sits at the root because the design notes' playlists point there. The
    # session gate above covers it.
    app.include_router(playback_api.media_router)
    # Mounted at the root: the design notes' API table names this socket /live/ws, and
    # it checks the session cookie itself.
    app.include_router(live_api.ws_router)

    static_dir: Path = resolved.static_dir
    assets = static_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")
    fonts = static_dir / "fonts"
    if fonts.is_dir():
        app.mount("/fonts", StaticFiles(directory=fonts), name="fonts")

    @app.get("/video-rtc.js")
    def player():
        """go2rtc's player, vendored into the image at build time."""
        script = static_dir / "video-rtc.js"
        if script.is_file():
            return FileResponse(script, media_type="application/javascript")
        return JSONResponse({"error": "video-rtc.js was not bundled."}, status_code=404)

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        """Serve the single-page shell for every non-API path.

        Registered last, so it only runs for paths no router claimed. /api is
        excluded explicitly: an unknown endpoint has to answer 404 JSON, not a
        200 of the HTML shell, or every client-side bug looks like a blank page.
        """
        if full_path == "api" or full_path.startswith("api/"):
            return JSONResponse({"error": "Not found."}, status_code=404)
        index = static_dir / "index.html"
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-store"})
        return HTMLResponse(_NO_FRONTEND.replace("__STATIC_DIR__", str(static_dir)))

    return app


# Uvicorn imports `nightwire.main:app`. Tests set NVR_SKIP_AUTOSTART=1 and
# call create_app() with their own settings instead.
app = None if os.environ.get("NVR_SKIP_AUTOSTART") == "1" else create_app()
