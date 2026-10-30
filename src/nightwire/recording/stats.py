"""Write-rate and retention projection per camera.

requires the recorder to track each camera's real
write rate, show projected days of retention from the last 7 days' average,
and warn when a camera projects under its `retention_days`.

**The projection model, stated plainly:** all cameras share one volume and
retention deletes oldest-first across every camera, so the number of days the
disk can hold is a property of the volume and the *combined* write rate, not
of any single camera. Each camera therefore reports the same projected days,
alongside its own measured bytes per day. That is the honest arithmetic; a
per-camera projection would imply per-camera quotas, which do not exist.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

WINDOW_DAYS = 7
BYTES_PER_GB = 1000**3


@dataclass(frozen=True)
class CameraRate:
    camera_id: str
    bytes_per_day: float
    mbps: float
    segment_count: int
    measured_days: float

    @property
    def gb_per_day(self) -> float:
        return self.bytes_per_day / BYTES_PER_GB


def camera_rates(
    conn: sqlite3.Connection, *, now_ms: int, window_days: int = WINDOW_DAYS
) -> list[CameraRate]:
    """Measured write rate per camera over the recent window.

    Measured against the span actually covered rather than the nominal window,
    so a camera added yesterday is not reported as writing a seventh of its
    real rate.
    """
    since = now_ms - window_days * 86_400_000
    rows = conn.execute(
        """
        SELECT camera_id,
               COUNT(*)        AS segments,
               SUM(size_bytes) AS bytes,
               MIN(start_ts)   AS first_ts,
               MAX(end_ts)     AS last_ts
        FROM segments
        WHERE start_ts >= ?
        GROUP BY camera_id
        ORDER BY camera_id
        """,
        (since,),
    )

    rates: list[CameraRate] = []
    for row in rows:
        total_bytes = float(row["bytes"] or 0)
        span_ms = max(0, int(row["last_ts"] or 0) - int(row["first_ts"] or 0))
        days = span_ms / 86_400_000
        if days < 0.01:  # under ~15 minutes of footage: not enough to average
            rates.append(
                CameraRate(row["camera_id"], 0.0, 0.0, int(row["segments"] or 0), round(days, 4))
            )
            continue
        bytes_per_day = total_bytes / days
        rates.append(
            CameraRate(
                camera_id=row["camera_id"],
                bytes_per_day=bytes_per_day,
                mbps=bytes_per_day * 8 / 86_400 / 1_000_000,
                segment_count=int(row["segments"] or 0),
                measured_days=round(days, 3),
            )
        )
    return rates


def projected_days(usable_bytes: float, rates: list[CameraRate]) -> float | None:
    """Days of footage the volume holds at the current combined write rate."""
    combined = sum(rate.bytes_per_day for rate in rates)
    if combined <= 0:
        return None
    return usable_bytes / combined


def projection(
    conn: sqlite3.Connection,
    *,
    now_ms: int,
    total_bytes: int,
    max_disk_percent: float,
    retention_days: dict[str, int],
    window_days: int = WINDOW_DAYS,
) -> dict:
    """Per-camera rates, the shared projection, and any shortfall warnings."""
    rates = camera_rates(conn, now_ms=now_ms, window_days=window_days)
    usable = total_bytes * (max_disk_percent / 100.0)
    days = projected_days(usable, rates)

    warnings = []
    if days is not None:
        for rate in rates:
            target = retention_days.get(rate.camera_id)
            if rate.bytes_per_day > 0 and target and days < target:
                warnings.append(
                    {
                        "camera_id": rate.camera_id,
                        "projected_days": round(days, 1),
                        "retention_days": target,
                        "detail": (
                            f"{rate.camera_id} is writing {rate.gb_per_day:.1f} GB/day "
                            f"({rate.mbps:.1f} Mbps). At the combined rate the volume holds "
                            f"{days:.0f} days, short of its {target}-day retention. Lower this "
                            "camera's bitrate cap or add space."
                        ),
                    }
                )

    return {
        "window_days": window_days,
        "cameras": [
            {
                "camera_id": rate.camera_id,
                "gb_per_day": round(rate.gb_per_day, 2),
                "mbps": round(rate.mbps, 2),
                "segments": rate.segment_count,
                "measured_days": rate.measured_days,
            }
            for rate in rates
        ],
        "combined_gb_per_day": round(sum(r.gb_per_day for r in rates), 2),
        "usable_bytes": int(usable),
        "projected_days": None if days is None else round(days, 1),
        "warnings": warnings,
    }
