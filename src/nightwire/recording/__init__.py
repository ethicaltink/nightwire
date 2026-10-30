"""Recording: the ffmpeg supervisor, indexer, retention and write-rate stats.

Split so that the parts worth testing can be tested without ffmpeg, a camera
or a disk:

- `paths`      segment layout, filenames, timestamps. Pure
- `ffmpeg`     argv construction and ffprobe output parsing. Pure
- `backoff`    restart schedule. Pure
- `retention`  what to delete (pure planning) and deleting it
- `stats`      measured write rate and retention projection
- `indexer`    finished-segment detection, insertion, startup reconcile
- `supervisor` the asyncio process management that ties it together
"""
