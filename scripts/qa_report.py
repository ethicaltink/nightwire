#!/usr/bin/env python3
"""Read-only health and consistency report for a running Nightwire install.

Run it inside the container, where it can see both the database and the
recordings tree:

    docker compose exec nvr python /usr/local/lib/python3.12/site-packages/nightwire/../../../../scripts/qa_report.py

or, more simply, with the script bind-mounted:

    docker compose exec nvr python /qa_report.py

It opens the database **read-only** and never writes, deletes, or calls the
API, so it is safe on a live system and needs no password.

What it looks for is what actually goes wrong on a real install, and in
particular what an unclean shutdown leaves behind: segments on disk that
were never indexed, index rows whose file has gone, events left open
forever, exports stuck "running" since a crash, retention running behind,
and camera clocks drifting away from the NVR's.

Exit code is 0 when nothing of concern was found, 1 when something was, so
it can be used as a check as well as read.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_DEFAULT = "/config/nightwire.db"
RECORDINGS_DEFAULT = "/recordings"
SEGMENT_SUFFIX = ".ts"
RESERVED_DIRS = ("_events", "_exports", "_thumbs")

MINUTE_MS = 60_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000

# An open event older than this is not motion, it is a crash: the tracker
# caps an open event at 10 minutes, so anything older outlived the process
# that was tracking it.
STALE_OPEN_MS = 15 * MINUTE_MS
# A gap shorter than this is ffmpeg restarting, not an outage worth listing.
GAP_FLOOR_MS = 5 * MINUTE_MS
# Overlaps under this are filename rounding, not two recorders.
OVERLAP_FLOOR_MS = 3_000
# A segment younger than this is the one ffmpeg is still writing, not a
# missed index.
UNINDEXED_GRACE_SECONDS = 300
# Beyond this a camera clock is drifting badly enough to misplace events.
CLOCK_WARN_MS = 2_000
# A clock reading older than this is stale: the listener remeasures hourly.
CLOCK_STALE_MS = 6 * HOUR_MS

findings: list[tuple[str, str]] = []


def note(level: str, text: str) -> None:
    findings.append((level, text))


def heading(text: str) -> None:
    print(f"\n{text}\n{'-' * len(text)}")


def when(ms: int | None) -> str:
    if not ms:
        return "never"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def span(ms: int) -> str:
    if ms >= DAY_MS:
        return f"{ms / DAY_MS:.1f}d"
    if ms >= HOUR_MS:
        return f"{ms / HOUR_MS:.1f}h"
    # Seconds up to two minutes: a 79s segment reading "1m" hid exactly the
    # detail the finding was about.
    if ms >= 2 * MINUTE_MS:
        return f"{ms / MINUTE_MS:.0f}m"
    return f"{ms / 1000:.0f}s"


def size(bytes_: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(bytes_) < 1024:
            return f"{bytes_:.1f}{unit}"
        bytes_ /= 1024
    return f"{bytes_:.1f}PB"


def open_db(path: Path) -> sqlite3.Connection:
    if not path.exists():
        sys.exit(f"No database at {path}. Pass --db if it lives elsewhere.")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------- checks


def report_install(conn: sqlite3.Connection, db_path: Path) -> None:
    heading("Install")
    print(f"database        {db_path} ({size(db_path.stat().st_size)})")
    for suffix in ("-wal", "-shm"):
        side = db_path.with_name(db_path.name + suffix)
        if side.exists():
            print(f"                {side.name} ({size(side.stat().st_size)})")
    try:
        applied = [
            row[0] for row in conn.execute("SELECT name FROM schema_migrations ORDER BY name")
        ]
        print(f"migrations      {len(applied)}: {', '.join(applied)}")
    except sqlite3.Error:
        print("migrations      table not found (older schema)")

    integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
    print(f"quick_check     {integrity}")
    if integrity != "ok":
        note("FAIL", f"SQLite quick_check reports {integrity!r}. Take a backup before anything else.")

    print(f"local time      {datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z')} (TZ={os.environ.get('TZ', 'unset')})")
    print(f"UTC             {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")

    settings = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}
    for key in sorted(settings):
        print(f"  {key:22s} {settings[key]}")
    return settings


def report_cameras(conn: sqlite3.Connection, now_ms: int) -> list[sqlite3.Row]:
    heading("Cameras")
    cameras = list(conn.execute("SELECT * FROM cameras ORDER BY sort_order, id"))
    if not cameras:
        note("WARN", "No cameras configured.")
        print("none")
        return cameras

    for camera in cameras:
        keys = camera.keys()
        offset = camera["clock_offset_ms"] if "clock_offset_ms" in keys else None
        checked = camera["clock_checked_at"] if "clock_checked_at" in keys else None
        bits = [
            f"{camera['id']:12s}",
            f"{camera['name'][:18]:18s}",
            f"{camera['host']:15s}",
            "recording" if camera["record"] else "NOT recording",
            f"keep {camera['retention_days']}d",
        ]
        if offset is None:
            bits.append("clock unmeasured")
        else:
            bits.append(f"clock {offset:+d}ms @ {when(checked)}")
        print("  " + " | ".join(bits))

        if offset is not None and abs(offset) > CLOCK_WARN_MS:
            note(
                "WARN",
                f"{camera['id']}: camera clock is {span(abs(offset))} "
                f"{'ahead of' if offset > 0 else 'behind'} the NVR. Event times are corrected "
                "for this, but check the camera's NTP settings.",
            )
        if checked and now_ms - checked > CLOCK_STALE_MS:
            note(
                "WARN",
                f"{camera['id']}: clock last measured {when(checked)}, over "
                f"{span(now_ms - checked)} ago. The listener remeasures hourly, so this "
                "suggests its event subscription is not running.",
            )
        if not camera["password_enc"]:
            note("INFO", f"{camera['id']}: no stored password (unauthenticated camera).")
    return cameras


def report_footage(conn: sqlite3.Connection, cameras, now_ms: int, days: int) -> None:
    heading(f"Footage (last {days} days)")
    since = now_ms - days * DAY_MS
    for camera in cameras:
        rows = list(
            conn.execute(
                "SELECT start_ts, end_ts, duration_ms, size_bytes, path FROM segments "
                "WHERE camera_id = ? AND end_ts >= ? ORDER BY start_ts",
                (camera["id"], since),
            )
        )
        if not rows:
            if camera["record"]:
                note("FAIL", f"{camera['id']}: recording is on but there are no segments in {days} days.")
            print(f"  {camera['id']:12s} no segments")
            continue

        total_ms = sum(row["duration_ms"] for row in rows)
        total_bytes = sum(row["size_bytes"] for row in rows)
        first, last = rows[0], rows[-1]
        window = max(1, min(now_ms, last["end_ts"]) - max(since, first["start_ts"]))
        print(
            f"  {camera['id']:12s} {len(rows):6d} segments  {span(total_ms):>7s} recorded  "
            f"{size(total_bytes):>8s}  {total_ms / window:5.1%} coverage"
        )
        print(f"               {when(first['start_ts'])}  ->  {when(last['end_ts'])}")

        # Silence since the last segment: the recorder should never be more
        # than a segment or two behind.
        behind = now_ms - last["end_ts"]
        if camera["record"] and behind > 10 * MINUTE_MS:
            note(
                "FAIL",
                f"{camera['id']}: newest footage ends {when(last['end_ts'])}, "
                f"{span(behind)} ago. Recording has stopped.",
            )

        gaps = []
        for previous, current in zip(rows, rows[1:]):
            hole = current["start_ts"] - previous["end_ts"]
            if hole >= GAP_FLOOR_MS:
                gaps.append((previous["end_ts"], current["start_ts"], hole))
        if gaps:
            print(f"               {len(gaps)} gap(s) over {span(GAP_FLOOR_MS)}:")
            for start, end, hole in gaps[-8:]:
                print(f"                 {when(start)} -> {when(end)}  ({span(hole)})")
            longest = max(hole for _s, _e, hole in gaps)
            note(
                "INFO",
                f"{camera['id']}: {len(gaps)} gap(s) in {days} days, longest {span(longest)}. "
                "Expected after a power cut; unexpected otherwise.",
            )

        # A segment whose duration is far from its slot is a truncated one,
        # which is what a power cut leaves at the end of each camera's day.
        short = [r for r in rows if r["duration_ms"] < 5_000]
        if short:
            note(
                "WARN",
                f"{camera['id']}: {len(short)} segment(s) under 5s, e.g. "
                f"{short[-1]['path']} ({short[-1]['duration_ms']}ms). Usually the partial "
                "segment left when the recorder was killed mid-write.",
            )
        # A segment filename has one-second resolution and its duration is
        # measured in milliseconds, so neighbours routinely overlap by a few
        # hundred ms. Only a real overlap is worth reporting: two recorders
        # at once, or an hour-sized one from a DST fall-back.
        overlaps = [
            (p["path"], c["path"], p["end_ts"] - c["start_ts"])
            for p, c in zip(rows, rows[1:])
            if c["start_ts"] < p["end_ts"] - OVERLAP_FLOOR_MS
        ]
        if overlaps:
            worst = max(overlap for _p, _c, overlap in overlaps)
            level = "FAIL" if worst > 30 * MINUTE_MS else "WARN"
            note(
                level,
                f"{camera['id']}: {len(overlaps)} segment pair(s) overlap by more than "
                f"{span(OVERLAP_FLOOR_MS)}, worst {span(worst)}, e.g. {overlaps[0][0]} and "
                f"{overlaps[0][1]}. An hour-sized overlap is the DST fall-back being "
                "misfiled; anything else means two recorders ran at once.",
            )

        long_cuts = [r for r in rows if r["duration_ms"] > 65_000]
        if long_cuts:
            longest = max(r["duration_ms"] for r in long_cuts)
            note(
                "INFO",
                f"{camera['id']}: {len(long_cuts)} segment(s) run past 65s, longest "
                f"{span(longest)}. A stream copy can only cut on a keyframe, so this is the "
                "camera's GOP being longer than the segment boundary - shorten its keyframe "
                "interval (or cap Zipstream's dynamic GOP) for tighter clips and seeks.",
            )


def report_disk(conn: sqlite3.Connection, recordings: Path, settings: dict) -> None:
    heading("Disk and index")
    if not recordings.is_dir():
        note("FAIL", f"No recordings directory at {recordings}.")
        print(f"  {recordings} missing")
        return

    usage = shutil.disk_usage(recordings)
    percent = usage.used / usage.total * 100
    print(
        f"  volume        {size(usage.used)} used of {size(usage.total)} "
        f"({percent:.1f}%), {size(usage.free)} free"
    )
    ceiling = float(settings.get("max_disk_percent", 90))
    if percent >= ceiling:
        note("FAIL", f"Disk is {percent:.1f}% full, at or past the {ceiling:.0f}% ceiling.")
    elif percent >= ceiling - 5:
        note("WARN", f"Disk is {percent:.1f}% full, approaching the {ceiling:.0f}% ceiling.")

    indexed = {row["path"] for row in conn.execute("SELECT path FROM segments")}
    on_disk = set()
    for path in recordings.rglob("*" + SEGMENT_SUFFIX):
        relative = path.relative_to(recordings).as_posix()
        if relative.split("/", 1)[0] in RESERVED_DIRS:
            continue
        on_disk.add(relative)

    print(f"  index         {len(indexed)} row(s), {len(on_disk)} file(s) on disk")

    missing = sorted(indexed - on_disk)
    if missing:
        note(
            "WARN",
            f"{len(missing)} indexed segment(s) have no file, e.g. {missing[0]}. "
            "Playback over those ranges will stall. Files deleted outside Nightwire?",
        )
    # The newest file per camera is legitimately the one ffmpeg is writing.
    unindexed = sorted(on_disk - indexed)
    empty, real = [], []
    settled = time.time() - UNINDEXED_GRACE_SECONDS
    for rel in unindexed:
        stat = (recordings / rel).stat()
        if stat.st_size == 0:
            empty.append(rel)
        elif stat.st_mtime < settled:
            # Younger than this it is simply the segment ffmpeg is writing.
            real.append(rel)
    if empty:
        note(
            "INFO",
            f"{len(empty)} zero-byte segment file(s), e.g. {empty[0]}. ffmpeg creates the "
            "file before writing to it, so these are what a kill mid-write leaves behind. "
            "The indexer skips them deliberately; they are safe to delete.",
        )
    if real:
        note(
            "WARN",
            f"{len(real)} finished segment file(s) are not in the index, e.g. {real[0]}. "
            "They exist but no page can find them; a startup reconcile should pick them up.",
        )

    for reserved in RESERVED_DIRS:
        folder = recordings / reserved
        if not folder.is_dir():
            continue
        files = list(folder.rglob("*"))
        bytes_ = sum(f.stat().st_size for f in files if f.is_file())
        print(f"  {reserved:13s} {sum(1 for f in files if f.is_file()):6d} file(s)  {size(bytes_)}")


def report_events(conn: sqlite3.Connection, recordings: Path, now_ms: int, days: int) -> None:
    heading(f"Motion events (last {days} days)")
    since = now_ms - days * DAY_MS
    sources = list(
        conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM events e WHERE e.source_id = s.id AND e.start_ts >= ?) "
            "AS recent FROM event_sources s ORDER BY s.camera_id, s.id",
            (since,),
        )
    )
    if not sources:
        note("INFO", "No event sources configured, so no motion is being recorded.")
        print("  none configured")
        return

    for source in sources:
        flag = "" if source["enabled"] else "  (disabled)"
        print(f"  {source['camera_id']:12s} {source['label'][:28]:28s} {source['recent']:6d} event(s){flag}")
        if source["enabled"] and source["recent"] == 0:
            note(
                "WARN",
                f"{source['camera_id']}/{source['label']}: enabled but no events in {days} days. "
                "Either nothing triggered it or the subscription is not delivering.",
            )

    stale = list(
        conn.execute(
            "SELECT e.id, e.camera_id, e.start_ts, s.label FROM events e "
            "LEFT JOIN event_sources s ON s.id = e.source_id "
            "WHERE e.end_ts IS NULL AND e.start_ts < ? ORDER BY e.start_ts",
            (now_ms - STALE_OPEN_MS,),
        )
    )
    if stale:
        note(
            "WARN",
            f"{len(stale)} event(s) are still open past the 10-minute cap, oldest "
            f"{when(stale[0]['start_ts'])} ({stale[0]['camera_id']}/{stale[0]['label']}). "
            "A crash or power cut left them unterminated; they draw as blocks that never end.",
        )
        for row in stale[:6]:
            print(f"    open since {when(row['start_ts'])}  {row['camera_id']}/{row['label']} (id {row['id']})")

    orphans = conn.execute(
        "SELECT COUNT(*) FROM events e LEFT JOIN event_sources s ON s.id = e.source_id "
        "WHERE s.id IS NULL"
    ).fetchone()[0]
    if orphans:
        note("WARN", f"{orphans} event(s) point at a source that no longer exists.")

    backwards = conn.execute(
        "SELECT COUNT(*) FROM events WHERE end_ts IS NOT NULL AND end_ts < start_ts"
    ).fetchone()[0]
    if backwards:
        note("FAIL", f"{backwards} event(s) end before they start.")

    missing_snaps = 0
    for row in conn.execute(
        "SELECT snapshot FROM events WHERE snapshot IS NOT NULL AND start_ts >= ?", (since,)
    ):
        if not (recordings / row["snapshot"]).exists():
            missing_snaps += 1
    if missing_snaps:
        note("INFO", f"{missing_snaps} event(s) reference a snapshot file that is gone.")

    # Drift between the camera's own stamp and the stored, corrected time.
    drifts = list(
        conn.execute(
            "SELECT camera_id, MAX(ABS(start_ts - camera_ts)) AS worst FROM events "
            "WHERE camera_ts IS NOT NULL AND start_ts >= ? GROUP BY camera_id",
            (since,),
        )
    )
    for row in drifts:
        if row["worst"] and row["worst"] > CLOCK_WARN_MS:
            note(
                "INFO",
                f"{row['camera_id']}: event times were corrected by up to {span(row['worst'])} "
                "from the camera's own clock.",
            )


def report_exports(conn: sqlite3.Connection, now_ms: int) -> None:
    heading("Exports")
    rows = list(conn.execute("SELECT status, COUNT(*) AS n FROM exports GROUP BY status"))
    if not rows:
        print("  none")
        return
    for row in rows:
        print(f"  {row['status']:10s} {row['n']}")

    stuck = list(
        conn.execute(
            "SELECT id, name, created_at FROM exports WHERE status IN ('running', 'queued') "
            "ORDER BY created_at"
        )
    )
    for row in stuck:
        try:
            created = datetime.fromisoformat(row["created_at"])
        except (TypeError, ValueError):
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - created > timedelta(hours=1):
            note(
                "WARN",
                f"Export {row['name'] or row['id']} has been queued or running since "
                f"{row['created_at']}. The worker was probably killed mid-job.",
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DB_DEFAULT, help=f"database path (default {DB_DEFAULT})")
    parser.add_argument(
        "--recordings", default=RECORDINGS_DEFAULT, help=f"recordings root (default {RECORDINGS_DEFAULT})"
    )
    parser.add_argument("--days", type=int, default=7, help="how far back to look (default 7)")
    args = parser.parse_args()

    db_path = Path(args.db)
    recordings = Path(args.recordings)
    now_ms = int(time.time() * 1000)

    print("Nightwire QA report")
    print(f"generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    conn = open_db(db_path)
    settings = report_install(conn, db_path)
    cameras = report_cameras(conn, now_ms)
    report_footage(conn, cameras, now_ms, args.days)
    report_disk(conn, recordings, settings or {})
    report_events(conn, recordings, now_ms, args.days)
    report_exports(conn, now_ms)

    heading("Findings")
    if not findings:
        print("  nothing of concern")
        return 0
    for level in ("FAIL", "WARN", "INFO"):
        for found, text in findings:
            if found == level:
                print(f"  [{level}] {text}")
    worst = "FAIL" if any(level == "FAIL" for level, _ in findings) else "WARN"
    return 1 if worst == "FAIL" or any(level == "WARN" for level, _ in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
