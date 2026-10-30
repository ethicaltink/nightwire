"""Camera discovery, probing and management.

Every route here requires a session; the gate in main.py runs ahead of
routing. Failures are mapped to honest status codes: 400 for bad input, 404
for an unknown camera, 502 when a camera refuses or times out, and 503 when
an optional dependency is missing from this install.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from .. import cameras as repo
from .. import crypto, recorders
from ..live import client as live_client
from ..live import config as live_config
from ..onvif import client as onvif
from ..onvif import parse, snapshot

log = logging.getLogger("nightwire.api.cameras")
router = APIRouter()

SNAPSHOT_CACHE_ATTR = "snapshot_uris"


class ProbeBody(BaseModel):
    host: str
    port: int = 80
    username: str | None = None
    password: str | None = None


class CreateBody(BaseModel):
    name: str
    host: str
    id: str | None = None
    onvif_port: int = 80
    username: str | None = None
    password: str | None = None
    main_profile: str | None = None
    sub_profile: str | None = None
    main_uri: str | None = None
    sub_uri: str | None = None
    retention_days: int = 45
    record: bool = True
    model: str | None = None
    firmware: str | None = None
    serial: str | None = None
    event_source_count: int | None = None


class PatchBody(BaseModel):
    name: str | None = None
    host: str | None = None
    onvif_port: int | None = None
    username: str | None = None
    password: str | None = None
    clear_password: bool = False
    main_uri: str | None = None
    sub_uri: str | None = None
    main_profile: str | None = None
    sub_profile: str | None = None
    record: bool | None = None
    retention_days: int | None = None
    sort_order: int | None = None

    def changes(self) -> dict:
        data = self.model_dump(exclude_none=True)
        data.pop("password", None)
        data.pop("clear_password", None)
        return data


class DiscoverBody(BaseModel):
    timeout: float = Field(default=onvif.DISCOVERY_SECONDS, ge=1.0, le=15.0)


def _error(message: str, status: int, **extra) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


def _handle(exc: Exception) -> JSONResponse:
    """Map a domain exception to a response. One place, so every route agrees."""
    if isinstance(exc, repo.NotFound):
        return _error(f"No camera with id {exc.args[0]!r}.", 404)
    if isinstance(exc, repo.CameraError):
        return _error(str(exc), 400)
    if isinstance(exc, (onvif.OnvifUnavailable, snapshot.SnapshotUnavailable, crypto.CryptoUnavailable)):
        return _error(str(exc), 503, dependency_missing=True)
    if isinstance(exc, crypto.DecryptFailed):
        return _error(str(exc), 409)
    if isinstance(exc, (onvif.OnvifError, snapshot.SnapshotError)):
        return _error(str(exc), 502)
    raise exc


@router.post("/discover")
async def discover(body: DiscoverBody, request: Request) -> JSONResponse:
    """WS-Discovery sweep. Cameras already added are marked, not hidden."""
    try:
        found = await onvif.discover(body.timeout)
    except Exception as exc:  # noqa: BLE001 - mapped by _handle
        return _handle(exc)

    conn = request.app.state.db
    known_hosts = {row["host"] for row in conn.execute("SELECT host FROM cameras")}
    for device in found:
        device["already_added"] = device["host"] in known_hosts
    return JSONResponse(
        {
            "devices": found,
            "already_added": sum(1 for d in found if d["already_added"]),
            "note": "Discovery only sees cameras on the same network segment.",
        }
    )


@router.post("/cameras/probe")
async def probe(body: ProbeBody, request: Request) -> JSONResponse:
    """Read device info and media profiles, and suggest main and sub."""
    try:
        result = await onvif.probe(body.host, body.port, body.username, body.password)
    except Exception as exc:  # noqa: BLE001
        return _handle(exc)

    conn = request.app.state.db
    payload = result.as_dict()
    payload["suggested_id"] = repo.suggest_id(conn, result.device.model or body.host)
    return JSONResponse(payload)


@router.get("/cameras")
def list_cameras(request: Request) -> JSONResponse:
    return JSONResponse({"cameras": repo.list_cameras(request.app.state.db)})


@router.post("/cameras")
async def create_camera(body: CreateBody, request: Request) -> JSONResponse:
    conn = request.app.state.db
    secret = request.app.state.settings.secret_key

    camera_id = (body.id or "").strip() or repo.suggest_id(conn, body.name)
    main_uri = (body.main_uri or "").strip()
    sub_uri = (body.sub_uri or "").strip()
    main_meta = sub_meta = None

    try:
        repo.validate_id(camera_id)

        # Resolve RTSP URIs from the chosen ONVIF profiles when they were not
        # supplied directly. GetStreamUri is preferred over building a URL by
        # hand, because the path convention differs by vendor and firmware.
        if not main_uri:
            if not body.main_profile:
                raise repo.CameraError(
                    "Choose a main stream profile, or supply main_uri directly."
                )
            main_uri = await onvif.stream_uri(
                body.host, body.onvif_port, body.username, body.password, body.main_profile
            )
        if not sub_uri and body.sub_profile:
            sub_uri = await onvif.stream_uri(
                body.host, body.onvif_port, body.username, body.password, body.sub_profile
            )

        # Keep the profile summary the Nodes page shows, so a card can be
        # drawn without a round trip to the camera.
        if body.main_profile or body.sub_profile:
            result = await onvif.probe(body.host, body.onvif_port, body.username, body.password)
            by_token = {p.token: p for p in result.profiles}
            main_meta = by_token[body.main_profile].as_dict() if body.main_profile in by_token else None
            sub_meta = by_token[body.sub_profile].as_dict() if body.sub_profile in by_token else None

        camera = repo.create(
            conn,
            secret,
            camera_id=camera_id,
            name=body.name,
            host=body.host,
            onvif_port=body.onvif_port,
            username=body.username,
            password=body.password,
            main_uri=main_uri,
            sub_uri=sub_uri or None,
            model=body.model,
            firmware=body.firmware,
            serial=body.serial,
            retention_days=body.retention_days,
            record=body.record,
            main_profile=body.main_profile,
            sub_profile=body.sub_profile,
            main_meta=main_meta,
            sub_meta=sub_meta,
            event_source_count=body.event_source_count,
        )
    except Exception as exc:  # noqa: BLE001
        return _handle(exc)

    await _resync_live(request)
    log.info("camera %s added (%s)", camera_id, body.host)
    return JSONResponse({"camera": camera}, status_code=201)


async def _resync_live(request: Request) -> None:
    """Tell go2rtc **and the recorder** about a camera change.

    Neither may fail the request: a camera must stay editable however badly
    live video or one recorder is behaving. `sync_quietly` already swallows a
    go2rtc fault; the blanket catches here are the boundary.

    The recorder half was missing until 2026-10-30. Recorders were only ever
    spawned at startup, so **a camera added through the UI recorded nothing
    until the container was restarted**, switching recording off left ffmpeg
    running, and deleting a camera left its recorder pulling the stream.
    """
    supervisor = getattr(request.app.state, "go2rtc", None)
    if supervisor is not None:
        try:
            await supervisor.sync_quietly()
        except Exception as exc:  # noqa: BLE001 - deliberately total
            log.warning("live resync failed after a camera change: %s", exc)

    recorder = getattr(request.app.state, "supervisor", None)
    if recorder is not None:
        try:
            await recorder.reconcile()
        except Exception as exc:  # noqa: BLE001 - deliberately total
            log.warning("recorder reconcile failed after a camera change: %s", exc)


@router.get("/cameras/{camera_id}")
def read_camera(camera_id: str, request: Request) -> JSONResponse:
    try:
        return JSONResponse({"camera": repo.get(request.app.state.db, camera_id)})
    except Exception as exc:  # noqa: BLE001
        return _handle(exc)


@router.patch("/cameras/{camera_id}")
async def patch_camera(camera_id: str, body: PatchBody, request: Request) -> JSONResponse:
    try:
        camera = repo.update(
            request.app.state.db,
            request.app.state.settings.secret_key,
            camera_id,
            body.changes(),
            password=body.password,
            clear_password=body.clear_password,
        )
    except Exception as exc:  # noqa: BLE001
        return _handle(exc)
    _forget_snapshot_uri(request, camera_id)
    await _resync_live(request)
    return JSONResponse({"camera": camera})


@router.delete("/cameras/{camera_id}")
async def delete_camera(
    camera_id: str, request: Request, delete_recordings: bool = False
) -> JSONResponse:
    """Remove a camera. Footage is kept unless delete_recordings=true."""
    try:
        result = repo.delete(
            request.app.state.db,
            camera_id,
            recordings_dir=request.app.state.settings.recordings_dir,
            delete_recordings=delete_recordings,
        )
    except Exception as exc:  # noqa: BLE001
        return _handle(exc)
    _forget_snapshot_uri(request, camera_id)
    await _resync_live(request)
    log.info("camera %s removed (recordings deleted: %s)", camera_id, delete_recordings)
    return JSONResponse(result)


@router.get("/cameras/{camera_id}/status")
def camera_status(camera_id: str, request: Request) -> JSONResponse:
    """Recorder state, last segment time and recent stderr.

    With no supervisor running, this reports the probe
    result and says plainly that recording has not been built, rather than
    implying a camera is broken.
    """
    conn = request.app.state.db
    try:
        row = repo.get_row(conn, camera_id)
    except Exception as exc:  # noqa: BLE001
        return _handle(exc)

    state = recorders.status_for(camera_id, enabled=bool(row["record"]))
    return JSONResponse(
        {
            "camera_id": camera_id,
            "recorder": state,
            "last_probe_at": row["last_probe_at"],
            "last_error": row["last_error"],
        }
    )


@router.post("/cameras/{camera_id}/test")
async def test_camera(camera_id: str, request: Request) -> JSONResponse:
    """Re-probe a saved camera and store the result. The "Test connection" button."""
    conn = request.app.state.db
    secret = request.app.state.settings.secret_key
    try:
        row = repo.get_row(conn, camera_id)
        username, password = repo.credentials(conn, camera_id, secret)
        result = await onvif.probe(row["host"], row["onvif_port"], username, password)
    except Exception as exc:  # noqa: BLE001
        try:
            repo.record_probe(conn, camera_id, error=str(exc))
        except Exception:  # noqa: BLE001 - never mask the original failure
            pass
        return _handle(exc)

    by_token = {p.token: p for p in result.profiles}
    repo.record_probe(
        conn,
        camera_id,
        device=result.device.as_dict(),
        main_meta=by_token[row["main_profile"]].as_dict() if row["main_profile"] in by_token else None,
        sub_meta=by_token[row["sub_profile"]].as_dict() if row["sub_profile"] in by_token else None,
        event_source_count=result.event_source_count,
        error=None,
    )
    return JSONResponse({"ok": True, "probe": result.as_dict(), "camera": repo.get(conn, camera_id)})


def _snapshot_cache(request: Request) -> dict:
    state = request.app.state
    if not hasattr(state, SNAPSHOT_CACHE_ATTR):
        setattr(state, SNAPSHOT_CACHE_ATTR, {})
    return getattr(state, SNAPSHOT_CACHE_ATTR)


def _forget_snapshot_uri(request: Request, camera_id: str) -> None:
    _snapshot_cache(request).pop(camera_id, None)


@router.get("/cameras/{camera_id}/snapshot.jpg")
async def camera_snapshot(camera_id: str, request: Request):
    """A still frame from the camera.

    Preferred from go2rtc, which already holds the stream; the camera's own
    ONVIF snapshot URI is the fallback. That URI is cached per camera because
    resolving it costs a full ONVIF handshake.
    """
    conn = request.app.state.db
    secret = request.app.state.settings.secret_key
    cache = _snapshot_cache(request)

    # Preferred path, as the spec intends: go2rtc already holds the stream, so
    # a frame costs no extra connection to the camera. The fallback is the
    # ONVIF snapshot URI because go2rtc did not exist yet; that is now the
    # fallback for when live video is down.
    supervisor = getattr(request.app.state, "go2rtc", None)
    if supervisor is not None and supervisor.state == "running":
        try:
            camera = repo.get(conn, camera_id)
            image = await live_client.frame(live_config.grid_stream_for(camera))
            return Response(
                content=image, media_type="image/jpeg", headers={"Cache-Control": "no-store"}
            )
        except repo.NotFound as exc:
            return _handle(exc)
        except (live_client.LiveError, live_client.LiveUnavailable) as exc:
            log.debug("go2rtc frame failed for %s, falling back to ONVIF: %s", camera_id, exc)

    try:
        row = repo.get_row(conn, camera_id)
        username, password = repo.credentials(conn, camera_id, secret)
        uri = cache.get(camera_id)
        if not uri:
            profile = row["sub_profile"] or row["main_profile"]
            if not profile:
                raise repo.CameraError(
                    "This camera has no ONVIF profile recorded, so a snapshot URI "
                    "cannot be resolved. Run Test connection, or add the camera again."
                )
            uri = await onvif.snapshot_uri(row["host"], row["onvif_port"], username, password, profile)
            cache[camera_id] = uri
        image = await snapshot.fetch(uri, username, password)
    except Exception as exc:  # noqa: BLE001
        cache.pop(camera_id, None)
        return _handle(exc)

    return Response(
        content=image,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )
