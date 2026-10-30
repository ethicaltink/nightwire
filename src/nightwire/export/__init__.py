"""Clip export: planning, the ffmpeg commands, and the single-slot worker.

- `plan`   which segments, what offset, how long, what filename. Pure
- `ffmpeg` the copy and precise-cut command lines. Pure
- `worker` one job at a time, under `nice`, writing to /recordings/_exports

Exports are evidence: retention never deletes them. The `max_exports_gb` cap
is enforced by **refusing new jobs**, never by removing saved clips.
"""
