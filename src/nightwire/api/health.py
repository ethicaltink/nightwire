"""GET /api/health - the only endpoint that never requires a session."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import health

router = APIRouter()


@router.get("/health")
def get_health(request: Request) -> JSONResponse:
    healthy, body = health.report(request.app.state.db)
    return JSONResponse(body, status_code=200 if healthy else 503)
