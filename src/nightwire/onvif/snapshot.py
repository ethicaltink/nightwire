"""Fetch a still frame from a camera over HTTP.

Pulls a snapshot straight from the camera's ONVIF snapshot URI. Used as the
fallback when go2rtc is not running; when it is, a frame from it costs no
extra connection to the camera. A caller can switch this to
go2rtc's frame API, which avoids a second HTTP session per camera.

Cameras commonly answer the snapshot URI with HTTP digest auth, so basic auth is
tried only as a fallback.
"""

from __future__ import annotations

import logging

log = logging.getLogger("nightwire.snapshot")

TIMEOUT_SECONDS = 10.0
MAX_BYTES = 8 * 1024 * 1024


class SnapshotUnavailable(RuntimeError):
    """`httpx` is not installed."""


class SnapshotError(RuntimeError):
    """The camera did not return an image."""


def _require_httpx():
    try:
        import httpx
    except ModuleNotFoundError as exc:
        raise SnapshotUnavailable(
            "Snapshots need `httpx`. Install it with `pip install -e .` or "
            "rebuild the container image."
        ) from exc
    return httpx


async def fetch(uri: str, username: str | None, password: str | None) -> bytes:
    """GET a JPEG, trying digest auth first and then basic.

    Returns the image bytes. Raises SnapshotError with the status code or the
    transport error, so the Nodes page can show why a camera has no preview
    rather than an empty tile.
    """
    httpx = _require_httpx()

    attempts = []
    if username:
        attempts.append(httpx.DigestAuth(username, password or ""))
        attempts.append(httpx.BasicAuth(username, password or ""))
    else:
        attempts.append(None)

    last_error = "no attempt made"
    for auth in attempts:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=True) as client:
                response = await client.get(uri, auth=auth)
            if response.status_code == 200:
                content = response.content
                if not content:
                    last_error = "camera returned an empty body"
                    continue
                if len(content) > MAX_BYTES:
                    raise SnapshotError(f"snapshot larger than {MAX_BYTES} bytes")
                return content
            last_error = f"HTTP {response.status_code}"
            # 401 is worth retrying with the other auth scheme; nothing else is.
            if response.status_code != 401:
                break
        except SnapshotError:
            raise
        except Exception as exc:  # noqa: BLE001 - httpx raises many transport errors
            last_error = str(exc)
            break

    raise SnapshotError(f"Snapshot failed: {last_error}")
