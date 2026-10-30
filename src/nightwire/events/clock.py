"""Camera clock offset correction. Pure, standard library only.

Event times come from the camera's own `UtcTime`; segment times come from the
NVR clock. The Rewind timeline draws camera-clocked events over NVR-clocked
footage, so **an uncorrected skew silently misplaces every marker** - a
camera two minutes fast puts its motion blocks two minutes before the footage
that caused them, and nothing on screen says so.

So: measure the offset per camera from ONVIF `GetSystemDateAndTime`, correct
event timestamps onto the NVR clock before storing them, keep the raw camera
time in the row for auditing, and surface the offset on Core.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

# Beyond this the camera's clock is wrong enough to be worth a warning, per
# .
WARN_THRESHOLD_MS = 2000

# Beyond this the reading is not a clock skew, it is a misconfiguration (a
# camera with no NTP sitting at its factory date). Correcting by it would be
# worse than ignoring it, so events are stored on the NVR clock instead.
IMPLAUSIBLE_MS = 86_400_000  # one day

REFRESH_SECONDS = 3600


@dataclass(frozen=True)
class Offset:
    """How far ahead of the NVR a camera's clock is, in milliseconds."""

    camera_id: str
    offset_ms: int
    measured_at_ms: int
    round_trip_ms: int = 0

    @property
    def plausible(self) -> bool:
        return abs(self.offset_ms) < IMPLAUSIBLE_MS

    @property
    def warn(self) -> bool:
        return abs(self.offset_ms) > WARN_THRESHOLD_MS

    @property
    def usable_offset_ms(self) -> int:
        """The correction actually applied. Zero when the reading is absurd."""
        return self.offset_ms if self.plausible else 0

    def as_dict(self) -> dict:
        return {
            "camera_id": self.camera_id,
            "offset_ms": self.offset_ms,
            "applied_ms": self.usable_offset_ms,
            "measured_at_ms": self.measured_at_ms,
            "round_trip_ms": self.round_trip_ms,
            "plausible": self.plausible,
            "warn": self.warn,
            "detail": self.detail,
        }

    @property
    def detail(self) -> str:
        if not self.plausible:
            return (
                f"{self.camera_id} reports a time {self.offset_ms / 1000:.0f}s from this host, "
                "which is too far to be clock drift. Event times are being stored on the NVR "
                "clock instead. Set NTP and the time zone on the camera."
            )
        if self.warn:
            direction = "ahead of" if self.offset_ms > 0 else "behind"
            return (
                f"{self.camera_id}'s clock is {abs(self.offset_ms) / 1000:.1f}s {direction} "
                "this host. Event times are corrected, but set NTP on the camera."
            )
        return f"{self.camera_id}'s clock agrees with this host to within {WARN_THRESHOLD_MS}ms."


def measure(
    camera_id: str, camera_utc_ms: int, sent_at_ms: int, received_at_ms: int
) -> Offset:
    """Offset from one `GetSystemDateAndTime` exchange.

    The camera's reading is compared against the midpoint of the request, so
    network latency is split rather than counted entirely as skew.
    """
    midpoint = (sent_at_ms + received_at_ms) // 2
    return Offset(
        camera_id=camera_id,
        offset_ms=int(camera_utc_ms - midpoint),
        measured_at_ms=received_at_ms,
        round_trip_ms=max(0, received_at_ms - sent_at_ms),
    )


def correct(camera_ts_ms: int, offset: Offset | None) -> int:
    """Move a camera timestamp onto the NVR clock."""
    if offset is None:
        return camera_ts_ms
    return camera_ts_ms - offset.usable_offset_ms


def parse_utc(value) -> int | None:
    """Milliseconds from an ONVIF time, in any of the shapes it arrives as.

    `GetSystemDateAndTime` returns a structured UTCDateTime; a PullMessages
    notification carries an ISO-8601 `UtcTime` attribute. Both end up here.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Already epoch-ish: milliseconds if it is large, else seconds.
        number = float(value)
        return int(number if number > 1e11 else number * 1000)
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(moment.timestamp() * 1000)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return int(moment.timestamp() * 1000)
    if isinstance(value, dict):
        # The structured form: {"Date": {...}, "Time": {...}}
        date = value.get("Date") or {}
        clock = value.get("Time") or {}
        try:
            moment = datetime(
                int(date["Year"]),
                int(date["Month"]),
                int(date["Day"]),
                int(clock.get("Hour", 0)),
                int(clock.get("Minute", 0)),
                int(clock.get("Second", 0)),
                tzinfo=timezone.utc,
            )
        except (KeyError, TypeError, ValueError):
            return None
        return int(moment.timestamp() * 1000)
    return None
