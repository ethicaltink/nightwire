"""Export endpoints: create a clip, poll it, download it, delete it."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .. import cameras as repo
from ..export import plan as plan_mod
from ..export import worker as worker_mod
from ..recording import paths

log = logging.getLogger("nightwire.api.exports")
router = APIRouter()


class CreateBody(BaseModel):
    camera_id: str
    start_ts: int
    end_ts: int
    precise: bool = False


def _error(message: str, status: int, **extra) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


def _worker(request: Request):
    return getattr(request.app.state, "exports", None)


@router.get("/exports")
def list_exports(request: Request) -> JSONResponse:
    """Everything in the Vault, newest first, plus the store's usage."""
    conn = request.app.state.db
    worker = _worker(request)
    return JSONResponse(
        {
            "exports": worker_mod.list_exports(conn),
            "status": worker.status()
            if worker is not None
            else {"running": None, "queued": 0, "done": 0, "failed": 0},
        }
    )


@router.post("/exports")
def create_export(body: CreateBody, request: Request) -> JSONResponse:
    """Queue a clip. Returns immediately; the UI polls for progress."""
    conn = request.app.state.db
    worker = _worker(request)
    if worker is None:
        return _error("The export worker is not running in this process.", 503)
    try:
        repo.get_row(conn, body.camera_id)
        created = worker.submit(
            body.camera_id, body.start_ts, body.end_ts, precise=body.precise
        )
    except repo.NotFound:
        return _error(f"No camera with id {body.camera_id!r}.", 404)
    except plan_mod.ExportError as exc:
        return _error(str(exc), 400)
    return JSONResponse({"export": created}, status_code=201)


@router.get("/exports/{export_id}")
def read_export(export_id: str, request: Request) -> JSONResponse:
    found = worker_mod.get_export(request.app.state.db, export_id)
    if found is None:
        return _error(f"No export {export_id!r}.", 404)
    return JSONResponse({"export": found})


@router.delete("/exports/{export_id}")
def delete_export(export_id: str, request: Request) -> JSONResponse:
    """Remove a clip. The only thing in Nightwire that deletes an export."""
    worker = _worker(request)
    if worker is None:
        return _error("The export worker is not running in this process.", 503)
    try:
        result = worker.delete(export_id)
    except LookupError:
        return _error(f"No export {export_id!r}.", 404)
    except plan_mod.ExportError as exc:
        return _error(str(exc), 409)
    log.info("export %s deleted", export_id)
    return JSONResponse(result)


@router.get("/exports/{export_id}/download")
def download_export(export_id: str, request: Request):
    """The finished MP4, as an attachment under its readable name."""
    conn = request.app.state.db
    settings = request.app.state.settings
    found = worker_mod.get_export(conn, export_id)
    if found is None:
        return _error(f"No export {export_id!r}.", 404)
    if found["status"] != worker_mod.DONE or not found.get("path"):
        return _error(
            f"That export is {found['status']}, so there is nothing to download yet.", 409
        )

    absolute = paths.absolute_path(settings.recordings_dir, found["path"])
    root = paths.exports_dir(settings.recordings_dir).resolve()
    # Only ever serve from inside _exports, whatever the row says.
    if root not in absolute.resolve().parents or not absolute.is_file():
        return _error("That export's file is missing.", 404)

    return FileResponse(
        absolute,
        media_type="video/mp4",
        filename=found.get("name") or absolute.name,
        headers={"Cache-Control": "private, max-age=3600"},
    )
