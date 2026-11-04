#!/usr/bin/env python3
"""Restamp segments misfiled by a DST fall-back.

**What went wrong.** ffmpeg names a segment from local wall-clock time, and
the autumn fall-back repeats an hour of it: on 2026-11-01 in America/Chicago,
`20261101T013000` named two instants an hour apart. The indexer resolved that
name with `datetime.replace(tzinfo=tz)`, which silently picks the earlier one,
so every segment from the second pass was stamped an hour early - on top of
the hour that was already there. The timeline then shows a 60-minute gap where
the second pass really was, and the playlist for that hour carries two
overlapping fragments at every instant.

`recording/paths.py` resolves the ambiguity from the file's mtime from
v0.1.6 on, so new recordings are right. This repairs what was already
indexed, by recomputing every segment's start from its filename and its
file's mtime and updating the rows that disagree.

It is a **one-off**, and it writes. Run it with the container stopped (the
app holds the database open, and a running recorder would be indexing while
this rewrites). It defaults to a dry run; pass `--apply` to make the change.

    docker compose stop nvr
    docker cp scripts/repair_dst_hour.py nightwire:/tmp/
    docker compose run --rm --entrypoint python nvr /tmp/repair_dst_hour.py
    docker compose run --rm --entrypoint python nvr /tmp/repair_dst_hour.py --apply
    docker compose start nvr

Only `segments.start_ts` and `segments.end_ts` change; nothing is deleted and
no file is touched. Events are not affected: they are stamped from the
camera's UTC clock, which has no DST.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/usr/local/lib/python3.12/site-packages")

try:
    from nightwire.recording import paths as paths_mod
except ImportError:  # pragma: no cover - run outside the image
    paths_mod = None

DB_DEFAULT = "/config/nightwire.db"
RECORDINGS_DEFAULT = "/recordings"
# Anything smaller is rounding, not a misfiled hour.
REPORT_FLOOR_MS = 60_000


def local_zone(name: str | None):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DB_DEFAULT)
    parser.add_argument("--recordings", default=RECORDINGS_DEFAULT)
    parser.add_argument("--tz", default=os.environ.get("TZ"), help="defaults to $TZ")
    parser.add_argument("--apply", action="store_true", help="write the corrections")
    args = parser.parse_args()

    if paths_mod is None:
        sys.exit("Run this inside the Nightwire container: it uses the app's own path rules.")

    tz = local_zone(args.tz)
    if tz is None:
        sys.exit(f"Could not resolve the timezone {args.tz!r}. Pass --tz.")

    recordings = Path(args.recordings)
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    rows = list(conn.execute("SELECT id, camera_id, path, start_ts, end_ts, duration_ms FROM segments"))
    print(f"{len(rows)} indexed segment(s), timezone {tz}")

    corrections = []
    missing = 0
    for row in rows:
        file_path = recordings / row["path"]
        try:
            mtime = file_path.stat().st_mtime
        except OSError:
            missing += 1
            continue
        correct = paths_mod.segment_start_ms(file_path, tz, mtime=mtime)
        drift = correct - row["start_ts"]
        if abs(drift) >= REPORT_FLOOR_MS:
            corrections.append((row, correct, drift))

    if missing:
        print(f"{missing} indexed segment(s) have no file and were left alone")

    if not corrections:
        print("Nothing to repair: every segment's timestamp already matches its file.")
        return 0

    by_camera: dict[str, int] = {}
    for row, _correct, _drift in corrections:
        by_camera[row["camera_id"]] = by_camera.get(row["camera_id"], 0) + 1
    print(f"\n{len(corrections)} segment(s) to restamp:")
    for camera_id in sorted(by_camera):
        print(f"  {camera_id:12s} {by_camera[camera_id]}")

    print("\nfirst few:")
    for row, correct, drift in corrections[:6]:
        was = datetime.fromtimestamp(row["start_ts"] / 1000, timezone.utc)
        now = datetime.fromtimestamp(correct / 1000, timezone.utc)
        print(f"  {row['path']}\n    {was:%Y-%m-%d %H:%M:%S} UTC -> {now:%Y-%m-%d %H:%M:%S} UTC ({drift / 3600000:+.2f}h)")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write these corrections.")
        return 0

    with conn:
        for row, correct, _drift in corrections:
            conn.execute(
                "UPDATE segments SET start_ts = ?, end_ts = ? WHERE id = ?",
                (correct, correct + row["duration_ms"], row["id"]),
            )
    print(f"\nRestamped {len(corrections)} segment(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
