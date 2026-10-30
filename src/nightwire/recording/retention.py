"""Retention: what to delete, then deleting it.

The planning functions are pure and take rows plus numbers, so the arithmetic
the spec asks to be unit-tested can be tested without a disk. `enforce` is
the only part that touches the filesystem.

Rules, from - delete segments older than each camera's retention_days
  - if the volume is above max_disk_percent, delete oldest-first across all
    cameras until below target_disk_percent
  - delete motion events and their snapshots with the footage they point at
  - never touch _exports/
  - remove empty hour and day folders afterwards
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

log = logging.getLogger("nightwire.retention")

INTERVAL_SECONDS = 600  # every 10 minutes
DEFAULT_RETENTION_DAYS = 45
DEFAULT_MAX_DISK_PERCENT = 90.0
DEFAULT_TARGET_DISK_PERCENT = 85.0
DEFAULT_MAX_EXPORTS_GB = 200.0


@dataclass(frozen=True)
class SegmentRow:
    """Just enough of a `segments` row to make a deletion decision."""

    id: int
    camera_id: str
    start_ts: int
    size_bytes: int
    path: str


@dataclass
class DiskUsage:
    total_bytes: int
    used_bytes: int
    free_bytes: int

    @property
    def used_percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return self.used_bytes / self.total_bytes * 100.0

    def percent_after_freeing(self, bytes_freed: int) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return max(0, self.used_bytes - bytes_freed) / self.total_bytes * 100.0


@dataclass
class Plan:
    by_age: list[SegmentRow] = field(default_factory=list)
    by_disk: list[SegmentRow] = field(default_factory=list)

    @property
    def all(self) -> list[SegmentRow]:
        seen: set[int] = set()
        ordered: list[SegmentRow] = []
        for row in [*self.by_age, *self.by_disk]:
            if row.id in seen:
                continue
            seen.add(row.id)
            ordered.append(row)
        return ordered

    @property
    def bytes_freed(self) -> int:
        return sum(row.size_bytes for row in self.all)

    def __bool__(self) -> bool:
        return bool(self.all)


def plan_by_age(
    rows: list[SegmentRow], now_ms: int, retention_days: dict[str, int], default_days: int
) -> list[SegmentRow]:
    """Segments older than their own camera's retention window."""
    doomed = []
    for row in rows:
        days = retention_days.get(row.camera_id, default_days)
        cutoff = now_ms - int(days) * 86_400_000
        if row.start_ts < cutoff:
            doomed.append(row)
    return sorted(doomed, key=lambda r: r.start_ts)


def plan_by_disk(
    rows: list[SegmentRow],
    usage: DiskUsage,
    max_percent: float,
    target_percent: float,
    already_freeing: int = 0,
) -> list[SegmentRow]:
    """Oldest-first across every camera, until the volume is under target.

    The guard only engages above `max_percent`. Deletions already planned by
    age count toward the target, so the two passes do not compound into
    deleting twice what is needed.
    """
    if usage.percent_after_freeing(already_freeing) <= max_percent:
        return []

    planned_ids = set()
    freed = already_freeing
    doomed: list[SegmentRow] = []
    for row in sorted(rows, key=lambda r: r.start_ts):
        if usage.percent_after_freeing(freed) <= target_percent:
            break
        if row.id in planned_ids:
            continue
        doomed.append(row)
        planned_ids.add(row.id)
        freed += row.size_bytes
    return doomed


def plan(
    rows: list[SegmentRow],
    usage: DiskUsage,
    *,
    now_ms: int,
    retention_days: dict[str, int],
    default_days: int = DEFAULT_RETENTION_DAYS,
    max_percent: float = DEFAULT_MAX_DISK_PERCENT,
    target_percent: float = DEFAULT_TARGET_DISK_PERCENT,
) -> Plan:
    by_age = plan_by_age(rows, now_ms, retention_days, default_days)
    aged_ids = {row.id for row in by_age}
    remaining = [row for row in rows if row.id not in aged_ids]
    by_disk = plan_by_disk(
        remaining,
        usage,
        max_percent,
        target_percent,
        already_freeing=sum(row.size_bytes for row in by_age),
    )
    return Plan(by_age=by_age, by_disk=by_disk)


def disk_usage(path: Path) -> DiskUsage:
    """Volume usage, or zeros when the volume is not there.

    Returning zeros rather than raising matters more than it looks: both
    /metrics and the Core page call this, and an exception would make
    Prometheus unable to scrape **precisely when the recordings volume has a
    problem**, so every alert would go blind at the worst moment. Zeros keep
    the scrape working, and `nightwire_recordings_volume_present` is what the
    alert fires on.
    """
    try:
        stats = shutil.disk_usage(path)
    except OSError as exc:
        log.error("recordings volume %s is unreadable: %s", path, exc)
        return DiskUsage(total_bytes=0, used_bytes=0, free_bytes=0)
    return DiskUsage(total_bytes=stats.total, used_bytes=stats.used, free_bytes=stats.free)


def volume_present(path: Path) -> bool:
    """Whether the recordings volume is actually there and readable."""
    try:
        return Path(path).is_dir() and shutil.disk_usage(path).total > 0
    except OSError:
        return False


def export_store_bytes(recordings_dir: Path) -> int:
    """Total size of saved clips. Exports are exempt from deletion here."""
    directory = paths.exports_dir(recordings_dir)
    if not directory.is_dir():
        return 0
    try:
        return sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
    except OSError as exc:
        log.warning("could not measure the export store: %s", exc)
        return 0


def export_cap_exceeded(recordings_dir: Path, max_exports_gb: float) -> tuple[bool, int, int]:
    """Whether new exports should be refused.

    The cap is enforced by **blocking new export jobs**, never by deleting
    saved clips: exports are evidence. Without the cap, enough saved clips
    quietly shorten footage retention for every camera through the disk
    guard, permanently and with no warning. The export worker calls this
    before accepting a job, and the Core page shows the usage.
    """
    used = export_store_bytes(recordings_dir)
    limit = int(max_exports_gb * 1000**3)
    return used >= limit, used, limit


def load_segments(conn: sqlite3.Connection) -> list[SegmentRow]:
    return [
        SegmentRow(
            id=row["id"],
            camera_id=row["camera_id"],
            start_ts=row["start_ts"],
            size_bytes=row["size_bytes"],
            path=row["path"],
        )
        for row in conn.execute(
            "SELECT id, camera_id, start_ts, size_bytes, path FROM segments ORDER BY start_ts"
        )
    ]


def retention_days_by_camera(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        row["id"]: int(row["retention_days"] or DEFAULT_RETENTION_DAYS)
        for row in conn.execute("SELECT id, retention_days FROM cameras")
    }


def _delete_events_for(conn: sqlite3.Connection, recordings_dir: Path, camera_id: str, before_ms: int) -> int:
    """Remove events, and their snapshots, that point at deleted footage."""
    rows = list(
        conn.execute(
            "SELECT id, snapshot FROM events WHERE camera_id = ? AND start_ts < ?",
            (camera_id, before_ms),
        )
    )
    for row in rows:
        if row["snapshot"]:
            snapshot = paths.absolute_path(recordings_dir, row["snapshot"])
            try:
                snapshot.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("could not delete event snapshot %s: %s", snapshot, exc)
    conn.execute(
        "DELETE FROM events WHERE camera_id = ? AND start_ts < ?", (camera_id, before_ms)
    )
    return len(rows)


def enforce(
    conn: sqlite3.Connection,
    recordings_dir: Path,
    *,
    now_ms: int,
    settings: dict[str, str] | None = None,
) -> dict:
    """Run one retention pass. Returns what it did."""
    settings = settings or {}

    def number(key: str, fallback: float) -> float:
        try:
            return float(settings.get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    rows = load_segments(conn)
    usage = disk_usage(recordings_dir)
    decided = plan(
        rows,
        usage,
        now_ms=now_ms,
        retention_days=retention_days_by_camera(conn),
        default_days=int(number("retention_days", DEFAULT_RETENTION_DAYS)),
        max_percent=number("max_disk_percent", DEFAULT_MAX_DISK_PERCENT),
        target_percent=number("target_disk_percent", DEFAULT_TARGET_DISK_PERCENT),
    )

    deleted_files = 0
    missing_files = 0
    touched_dirs: set[Path] = set()
    newest_deleted: dict[str, int] = {}

    for row in decided.all:
        absolute = paths.absolute_path(recordings_dir, row.path)
        try:
            if absolute.is_file():
                absolute.unlink()
                deleted_files += 1
            else:
                missing_files += 1
            touched_dirs.add(absolute.parent)
        except OSError as exc:
            log.warning("could not delete %s: %s", absolute, exc)
            continue

        thumb = paths.thumb_path(recordings_dir, row.camera_id, absolute)
        try:
            thumb.unlink(missing_ok=True)
        except OSError:
            pass

        conn.execute("DELETE FROM segments WHERE id = ?", (row.id,))
        end_of_segment = row.start_ts + 1
        newest_deleted[row.camera_id] = max(newest_deleted.get(row.camera_id, 0), end_of_segment)

    deleted_events = 0
    for camera_id, before_ms in newest_deleted.items():
        deleted_events += _delete_events_for(conn, recordings_dir, camera_id, before_ms)

    conn.commit()

    pruned = 0
    for directory in touched_dirs:
        pruned += paths.prune_empty_dirs(directory, recordings_dir)

    result = {
        "deleted_segments": len(decided.all),
        "deleted_by_age": len(decided.by_age),
        "deleted_by_disk": len(decided.by_disk),
        "deleted_files": deleted_files,
        "missing_files": missing_files,
        "deleted_events": deleted_events,
        "bytes_freed": decided.bytes_freed,
        "pruned_dirs": pruned,
        "used_percent_before": round(usage.used_percent, 2),
    }
    if decided:
        log.info("retention pass: %s", result)
        if decided.by_disk:
            log.warning(
                "disk guard engaged at %.1f%%: deleted %d segment(s) before their "
                "retention window expired. A camera's bitrate cap may be too high.",
                usage.used_percent,
                len(decided.by_disk),
            )
    return result
