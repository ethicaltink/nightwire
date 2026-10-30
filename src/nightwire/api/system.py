"""The Core page payload, the retention settings, and /metrics."""

from __future__ import annotations

import logging
import os
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from .. import __version__, metrics
from ..events import clock as clock_mod
from ..events import store as event_store
from ..recording import retention, stats

log = logging.getLogger("nightwire.api.system")
router = APIRouter()
metrics_router = APIRouter()

# Settings the Core page may change, with their sane bounds.
EDITABLE_SETTINGS = {
    "retention_days": (1, 3650),
    "max_disk_percent": (50, 99),
    "target_disk_percent": (10, 98),
    "segment_seconds": (10, 600),
    "max_exports_gb": (1, 100_000),
    "station_reload_hour": (0, 23),
}


class SettingsBody(BaseModel):
    retention_days: int | None = None
    max_disk_percent: float | None = None
    target_disk_percent: float | None = None
    max_exports_gb: float | None = None
    station_reload_hour: int | None = None

    def changes(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if v is not None}


def _error(message: str, status: int, **extra) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


def format_setting(value) -> str:
    """Store 70, not "70.0".

    The body types these as floats, so a whole number arrives as 70.0 and
    would be stored and read back as a different string than was sent - which
    also leaks into the metrics output.
    """
    number = float(value)
    return str(int(number)) if number == int(number) else str(number)


def _settings(conn) -> dict:
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}


@router.get("/settings")
def get_settings(request: Request) -> JSONResponse:
    return JSONResponse({"settings": _settings(request.app.state.db)})


@router.patch("/settings")
def patch_settings(body: SettingsBody, request: Request) -> JSONResponse:
    """Change a retention or capacity setting.

    Bounds are enforced here rather than trusted from the page: a
    `max_disk_percent` of 5 would have the guard delete almost everything,
    and a target above the ceiling would make it delete in a loop.
    """
    conn = request.app.state.db
    changes = body.changes()
    if not changes:
        return JSONResponse({"settings": _settings(conn)})

    for key, value in changes.items():
        low, high = EDITABLE_SETTINGS[key]
        if not low <= float(value) <= high:
            return _error(f"{key} must be between {low} and {high}.", 400)

    merged = {**_settings(conn), **{k: format_setting(v) for k, v in changes.items()}}
    try:
        if float(merged["target_disk_percent"]) >= float(merged["max_disk_percent"]):
            return _error(
                "target_disk_percent must be below max_disk_percent, or the retention "
                "guard would delete continuously once it engages.",
                400,
            )
    except (KeyError, TypeError, ValueError):
        return _error("Disk thresholds must be numbers.", 400)

    for key, value in changes.items():
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, format_setting(value)),
        )
    conn.commit()
    log.info("settings changed: %s", changes)
    return JSONResponse({"settings": _settings(conn), "changed": sorted(changes)})


@router.get("/system")
def system(request: Request) -> JSONResponse:
    """Everything the Core page shows."""
    state = request.app.state
    conn = state.db
    settings = _settings(conn)
    now_ms = int(time.time() * 1000)

    def number(key: str, fallback: float) -> float:
        try:
            return float(settings.get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    usage = retention.disk_usage(state.settings.recordings_dir)
    retention_days = retention.retention_days_by_camera(conn)
    projection = stats.projection(
        conn,
        now_ms=now_ms,
        total_bytes=usage.total_bytes,
        max_disk_percent=number("max_disk_percent", retention.DEFAULT_MAX_DISK_PERCENT),
        retention_days=retention_days,
    )

    # Per-camera disk share, for the stacked bar on Core.
    shares = []
    for row in conn.execute(
        "SELECT camera_id, SUM(size_bytes) AS bytes, COUNT(*) AS segments"
        " FROM segments GROUP BY camera_id ORDER BY camera_id"
    ):
        shares.append(
            {
                "camera_id": row["camera_id"],
                "bytes": row["bytes"] or 0,
                "segments": row["segments"],
                "percent": round((row["bytes"] or 0) / usage.total_bytes * 100, 2)
                if usage.total_bytes
                else 0.0,
            }
        )

    recorder = getattr(state, "supervisor", None)
    events = getattr(state, "events", None)
    go2rtc = getattr(state, "go2rtc", None)
    exports = getattr(state, "exports", None)

    cameras = []
    for row in conn.execute("SELECT * FROM cameras ORDER BY sort_order, id"):
        camera_id = row["id"]
        offset = event_store.load_offset(conn, camera_id)
        rate = next(
            (entry for entry in projection["cameras"] if entry["camera_id"] == camera_id), None
        )
        cameras.append(
            {
                "camera_id": camera_id,
                "name": row["name"],
                "record": bool(row["record"]),
                "retention_days": retention_days.get(camera_id),
                "rate": rate,
                "recorder": recorder.status(camera_id)
                if recorder is not None
                else {"state": "not-started", "detail": "the recorder is not running"},
                "events": events.status(camera_id)
                if events is not None
                else {"state": "stopped", "detail": "the event listener is not running"},
                "clock": offset.as_dict() if offset is not None else None,
                "clock_warn_threshold_ms": clock_mod.WARN_THRESHOLD_MS,
            }
        )

    return JSONResponse(
        {
            "version": __version__,
            "timezone": state.settings.timezone,
            "settings": settings,
            "disk": {
                "total_bytes": usage.total_bytes,
                "used_bytes": usage.used_bytes,
                "free_bytes": usage.free_bytes,
                "used_percent": round(usage.used_percent, 2),
                "max_percent": number("max_disk_percent", retention.DEFAULT_MAX_DISK_PERCENT),
                "target_percent": number(
                    "target_disk_percent", retention.DEFAULT_TARGET_DISK_PERCENT
                ),
                "per_camera": shares,
            },
            "projection": projection,
            "cameras": cameras,
            "exports": exports.status() if exports is not None else None,
            "go2rtc": go2rtc.status() if go2rtc is not None else None,
            "retention": getattr(recorder, "last_retention", None) if recorder else None,
        }
    )


@metrics_router.get("/metrics")
def prometheus_metrics(request: Request):
    """Prometheus scrape endpoint.

    **Deliberately reachable without a session**, because a scraper cannot
    hold a login cookie, and the spec puts `/metrics` outside `/api`. It
    exposes camera ids, bitrates and disk usage - never footage. Set
    `NVR_METRICS_TOKEN` to require `Authorization: Bearer <token>` if even
    that should be closed on your network.
    """
    token = os.environ.get("NVR_METRICS_TOKEN")
    if token:
        supplied = request.headers.get("authorization", "")
        if supplied != f"Bearer {token}":
            return PlainTextResponse("Unauthorized\n", status_code=401)

    state = request.app.state
    families = metrics.collect(
        state.db,
        state.settings.recordings_dir,
        now_ms=int(time.time() * 1000),
        version=__version__,
        recorder=getattr(state, "supervisor", None),
        go2rtc=getattr(state, "go2rtc", None),
        events=getattr(state, "events", None),
        exports=getattr(state, "exports", None),
    )
    return PlainTextResponse(
        metrics.render(families),
        media_type="text/plain; version=0.0.4; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )
