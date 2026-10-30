"""Playback: the timeline, the HLS VOD playlist, and segment delivery.

- `timeline` merges segments into recorded ranges and names the gaps. Pure
- `playlist` renders an HLS VOD playlist over those segments. Pure

Both are pure so the two things the spec asks to be unit-tested - playlist
generation with gaps, and timeline merging - are testable without a disk, a
camera or ffmpeg.
"""
