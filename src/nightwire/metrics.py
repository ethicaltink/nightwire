"""Prometheus metrics.

Rendering is pure and collection takes the state it needs as arguments, so
both are testable without a running supervisor.

**Every metric here exists because an alert rule needs it.** The lab this
runs on had Prometheus scraping node_exporter with no rules and no
Alertmanager, which is why three cameras recorded nothing for days in
October 2026 and nothing fired. `deploy/alerts.yml` is the deliverable;
these metrics are what it reads. A test asserts that every metric an alert
references is actually exported.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import db
from .events import store as event_store
from .recording import retention, stats

PREFIX = "nightwire"

GAUGE = "gauge"
COUNTER = "counter"


@dataclass
class Sample:
    value: float
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class Family:
    name: str
    help: str
    kind: str = GAUGE
    samples: list[Sample] = field(default_factory=list)

    def add(self, value, **labels) -> "Family":
        self.samples.append(Sample(float(value), {k: str(v) for k, v in labels.items()}))
        return self


def escape_label(value: str) -> str:
    """Prometheus label values escape backslash, quote and newline."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def render(families: list[Family]) -> str:
    """The Prometheus text exposition format."""
    lines: list[str] = []
    for family in families:
        if not family.samples:
            continue
        lines.append(f"# HELP {family.name} {family.help}")
        lines.append(f"# TYPE {family.name} {family.kind}")
        for sample in family.samples:
            if sample.labels:
                pairs = ",".join(
                    f'{key}="{escape_label(value)}"' for key, value in sorted(sample.labels.items())
                )
                lines.append(f"{family.name}{{{pairs}}} {format_value(sample.value)}")
            else:
                lines.append(f"{family.name} {format_value(sample.value)}")
    return "\n".join(lines) + "\n"


def format_value(value: float) -> str:
    """Integers without a decimal point, everything else at six places."""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


# The states each component can report, so a series exists for every one and
# an alert can match on absence as well as presence.
RECORDER_STATES = ("recording", "reconnecting", "error", "disabled", "stopped", "not-started")
LISTENER_STATES = ("subscribed", "reconnecting", "no sources", "stopped", "unavailable")


def collect(
    conn: sqlite3.Connection,
    recordings_dir: Path,
    *,
    now_ms: int,
    version: str = "0.1.0",
    recorder=None,
    go2rtc=None,
    events=None,
    exports=None,
) -> list[Family]:
    """Gather everything `deploy/alerts.yml` needs.

    Every supervisor is optional: a metric for a component that is not
    running is reported as 0 rather than omitted, so an alert on "not
    recording" fires instead of going blind.
    """
    settings = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}

    def number(key: str, fallback: float) -> float:
        try:
            return float(settings.get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    cameras = list(conn.execute("SELECT * FROM cameras ORDER BY sort_order, id"))
    enabled = [row for row in cameras if row["record"]]
    retention_days = retention.retention_days_by_camera(conn)
    usage = retention.disk_usage(recordings_dir)
    max_percent = number("max_disk_percent", retention.DEFAULT_MAX_DISK_PERCENT)
    target_percent = number("target_disk_percent", retention.DEFAULT_TARGET_DISK_PERCENT)
    cap_gb = number("max_exports_gb", retention.DEFAULT_MAX_EXPORTS_GB)

    projection = stats.projection(
        conn,
        now_ms=now_ms,
        total_bytes=usage.total_bytes,
        max_disk_percent=max_percent,
        retention_days=retention_days,
    )
    rates = {entry["camera_id"]: entry for entry in projection["cameras"]}

    families: list[Family] = []

    up = Family(f"{PREFIX}_up", "1 when the Nightwire process is serving.").add(1)
    families.append(up)
    families.append(
        Family(f"{PREFIX}_build_info", "Build information.").add(1, version=version)
    )
    families.append(
        Family(f"{PREFIX}_cameras_total", "Cameras configured.").add(len(cameras))
    )
    families.append(
        Family(f"{PREFIX}_cameras_enabled", "Cameras with recording switched on.").add(
            len(enabled)
        )
    )

    # ------------------------------------------------------------- recorders
    recording = Family(
        f"{PREFIX}_recorder_recording",
        "1 when this camera's recorder is writing segments, 0 otherwise.",
    )
    state_family = Family(
        f"{PREFIX}_recorder_state", "1 for the recorder state this camera is in."
    )
    since = Family(
        f"{PREFIX}_recorder_seconds_since_segment",
        "Seconds since this camera's last indexed segment.",
    )
    restarts = Family(
        f"{PREFIX}_recorder_restarts_total", "ffmpeg restarts for this camera.", COUNTER
    )
    watchdog = Family(
        f"{PREFIX}_recorder_watchdog_kills_total",
        "Times the stall watchdog restarted this camera's ffmpeg.",
        COUNTER,
    )

    for row in cameras:
        camera_id = row["id"]
        status = (
            recorder.status(camera_id)
            if recorder is not None
            else {"state": "not-started", "seconds_since_segment": None}
        )
        state = status.get("state") or "not-started"
        recording.add(1 if state == "recording" else 0, camera=camera_id)
        for candidate in RECORDER_STATES:
            state_family.add(1 if state == candidate else 0, camera=camera_id, state=candidate)
        seconds = status.get("seconds_since_segment")
        if seconds is not None:
            since.add(seconds, camera=camera_id)
        restarts.add(status.get("restarts", 0) or 0, camera=camera_id)
        watchdog.add(status.get("watchdog_kills", 0) or 0, camera=camera_id)

    families += [recording, state_family, since, restarts, watchdog]

    # ----------------------------------------------------------- write rates
    bytes_per_day = Family(
        f"{PREFIX}_camera_bytes_per_day", "Measured bytes written per day for this camera."
    )
    mbps = Family(f"{PREFIX}_camera_mbps", "Measured average bitrate in Mbps.")
    segments = Family(f"{PREFIX}_camera_segments_total", "Segments indexed for this camera.")
    configured = Family(
        f"{PREFIX}_camera_retention_days", "Configured retention in days for this camera."
    )
    projected = Family(
        f"{PREFIX}_projected_retention_days",
        "Days of footage the volume holds at the current combined write rate.",
    )

    for row in cameras:
        camera_id = row["id"]
        rate = rates.get(camera_id)
        if rate:
            bytes_per_day.add(rate["gb_per_day"] * 1000**3, camera=camera_id)
            mbps.add(rate["mbps"], camera=camera_id)
            segments.add(rate["segments"], camera=camera_id)
        configured.add(retention_days.get(camera_id, retention.DEFAULT_RETENTION_DAYS), camera=camera_id)
        if projection["projected_days"] is not None:
            # The projection is a property of the shared volume, reported per
            # camera so a rule can compare it against that camera's own
            # retention setting. See recording/stats.py for why.
            projected.add(projection["projected_days"], camera=camera_id)

    families += [bytes_per_day, mbps, segments, configured, projected]

    # ------------------------------------------------------------------ disk
    families.append(
        Family(
            f"{PREFIX}_recordings_volume_present",
            "1 when the recordings volume is mounted and readable.",
        ).add(1 if retention.volume_present(recordings_dir) else 0)
    )
    families.append(
        Family(f"{PREFIX}_disk_total_bytes", "Recordings volume size.").add(usage.total_bytes)
    )
    families.append(
        Family(f"{PREFIX}_disk_used_bytes", "Recordings volume used.").add(usage.used_bytes)
    )
    families.append(
        Family(f"{PREFIX}_disk_free_bytes", "Recordings volume free.").add(usage.free_bytes)
    )
    families.append(
        Family(f"{PREFIX}_disk_used_percent", "Recordings volume percent used.").add(
            round(usage.used_percent, 3)
        )
    )
    families.append(
        Family(
            f"{PREFIX}_disk_max_percent",
            "Configured ceiling at which the retention guard starts deleting.",
        ).add(max_percent)
    )
    families.append(
        Family(
            f"{PREFIX}_disk_target_percent", "Configured level the guard deletes down to."
        ).add(target_percent)
    )

    # --------------------------------------------------------------- exports
    export_status = exports.status() if exports is not None else None
    store_bytes = (
        export_status["store_bytes"]
        if export_status
        else retention.export_store_bytes(recordings_dir)
    )
    families.append(
        Family(f"{PREFIX}_exports_store_bytes", "Bytes of saved clips.").add(store_bytes)
    )
    families.append(
        Family(
            f"{PREFIX}_exports_store_cap_bytes",
            "Cap above which new export jobs are refused.",
        ).add(int(cap_gb * 1000**3))
    )
    families.append(
        Family(f"{PREFIX}_exports_queued", "Export jobs waiting to run.").add(
            export_status["queued"] if export_status else 0
        )
    )
    families.append(
        Family(f"{PREFIX}_exports_failed_total", "Export jobs that failed.", COUNTER).add(
            export_status["failed"] if export_status else 0
        )
    )
    families.append(
        Family(f"{PREFIX}_exports_running", "1 while an export is being made.").add(
            1 if (export_status and export_status["running"]) else 0
        )
    )

    # ------------------------------------------------------- event listeners
    subscribed = Family(
        f"{PREFIX}_events_subscribed",
        "1 when this camera's event listener is subscribed, 0 otherwise.",
    )
    listener_state = Family(
        f"{PREFIX}_event_listener_state", "1 for the listener state this camera is in."
    )
    sources_family = Family(
        f"{PREFIX}_event_sources_enabled", "Event sources chosen for this camera."
    )
    offset = Family(
        f"{PREFIX}_camera_clock_offset_seconds",
        "How far this camera's clock is from the NVR's. Positive means ahead.",
    )
    open_events = Family(f"{PREFIX}_events_open", "Motion events currently open.")

    for row in cameras:
        camera_id = row["id"]
        status = (
            events.status(camera_id)
            if events is not None
            else {"state": "stopped"}
        )
        state = status.get("state") or "stopped"
        subscribed.add(1 if state == "subscribed" else 0, camera=camera_id)
        for candidate in LISTENER_STATES:
            listener_state.add(1 if state == candidate else 0, camera=camera_id, state=candidate)
        sources_family.add(
            len(event_store.enabled_sources(conn, camera_id)), camera=camera_id
        )
        measured = event_store.load_offset(conn, camera_id)
        if measured is not None:
            offset.add(measured.offset_ms / 1000, camera=camera_id)

    open_events.add(len(event_store.open_now(conn)))
    families += [subscribed, listener_state, sources_family, offset, open_events]

    # ---------------------------------------------------------------- go2rtc
    go2rtc_state = (go2rtc.status() or {}) if go2rtc is not None else {}
    families.append(
        Family(f"{PREFIX}_go2rtc_running", "1 when go2rtc is running and answering.").add(
            1 if go2rtc_state.get("state") == "running" else 0
        )
    )
    families.append(
        Family(f"{PREFIX}_go2rtc_streams", "Streams registered with go2rtc.").add(
            len(go2rtc_state.get("streams") or [])
        )
    )

    return families


def metric_names(families: list[Family]) -> set[str]:
    return {family.name for family in families}


def exported_names() -> set[str]:
    """Every metric name this module can emit.

    Built by collecting against an empty in-memory database, so the alert
    rules can be checked against it without a running system.
    """
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    db.migrate(connection)
    # A camera, so the per-camera families produce samples and therefore names.
    connection.execute(
        "INSERT INTO cameras (id, name, host, main_uri, created_at)"
        " VALUES ('probe', 'probe', 'localhost', 'rtsp://x', '2026-01-01')"
    )
    connection.commit()
    try:
        families = collect(connection, Path("."), now_ms=0)
        return metric_names(families)
    finally:
        connection.close()
