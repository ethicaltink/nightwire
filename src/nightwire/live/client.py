"""Talking to go2rtc's HTTP API on localhost.

`httpx` is imported lazily, like every other optional dependency, so the app
starts and `/api/health` answers even if it is missing: only live video
stops working.
"""

from __future__ import annotations

import logging

from . import config

log = logging.getLogger("nightwire.live")

TIMEOUT_SECONDS = 5.0
FRAME_TIMEOUT_SECONDS = 10.0


class LiveUnavailable(RuntimeError):
    """`httpx` is not installed."""


class LiveError(RuntimeError):
    """go2rtc refused, timed out, or returned something unusable."""


def _require_httpx():
    try:
        import httpx
    except ModuleNotFoundError as exc:
        raise LiveUnavailable(
            "Live video needs `httpx`. Install it with `pip install -e .` or "
            "rebuild the container image."
        ) from exc
    return httpx


async def ping() -> dict:
    """go2rtc's own info endpoint. Used by the health probe."""
    httpx = _require_httpx()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.get(config.api_url("api"))
        if response.status_code != 200:
            raise LiveError(f"go2rtc answered HTTP {response.status_code}")
        return response.json()
    except LiveError:
        raise
    except Exception as exc:  # noqa: BLE001 - httpx raises many transport errors
        raise LiveError(f"go2rtc unreachable on {config.api_url()}: {exc}") from exc


async def list_streams() -> dict:
    httpx = _require_httpx()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.get(config.api_url("api/streams"))
        if response.status_code != 200:
            raise LiveError(f"go2rtc answered HTTP {response.status_code}")
        return response.json() or {}
    except LiveError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise LiveError(f"could not list go2rtc streams: {exc}") from exc


async def put_stream(name: str, source: str) -> None:
    """Register or replace one stream.

    `PUT /api/streams?name=<name>&src=<source>` is go2rtc's own shape. Called
    whenever a camera is added or edited, so the live side follows the
    database without a restart.
    """
    httpx = _require_httpx()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.put(
                config.api_url("api/streams"), params={"name": name, "src": source}
            )
        if response.status_code not in (200, 201, 204):
            raise LiveError(f"go2rtc refused stream {name}: HTTP {response.status_code}")
    except LiveError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise LiveError(f"could not register stream {name}: {exc}") from exc


async def delete_stream(name: str) -> None:
    httpx = _require_httpx()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            await client.delete(config.api_url("api/streams"), params={"src": name})
    except Exception as exc:  # noqa: BLE001 - removal failing is not fatal
        log.debug("could not delete stream %s: %s", name, exc)


async def sync_streams(cameras, *, secret: str = "") -> dict:
    """Make go2rtc's stream list match the database.

    Adds and replaces what should exist, and removes only streams that follow
    our own `<id>_main` / `<id>_sub` naming. A stream someone added by hand in
    go2rtc's own config is left alone.
    """
    # Sources carry camera credentials when `secret` is set; they travel only
    # over go2rtc's localhost API and are never logged. See live/config.py.
    wanted = {spec.name: spec.source for spec in config.streams_for(cameras, secret=secret)}
    existing = await list_streams()

    added, removed = [], []
    for name, source in wanted.items():
        await put_stream(name, source)
        if name not in existing:
            added.append(name)

    for name in existing:
        if name in wanted:
            continue
        if config.parse_stream_name(name) is None:
            continue  # not ours; leave it
        await delete_stream(name)
        removed.append(name)

    result = {"streams": sorted(wanted), "added": sorted(added), "removed": sorted(removed)}
    if added or removed:
        log.info("go2rtc streams synced: %s", result)
    return result


async def frame(stream: str) -> bytes:
    """A still JPEG from a live stream. The design's preferred snapshot path."""
    httpx = _require_httpx()
    try:
        async with httpx.AsyncClient(timeout=FRAME_TIMEOUT_SECONDS) as client:
            response = await client.get(config.frame_url(stream))
        if response.status_code != 200:
            raise LiveError(f"go2rtc frame for {stream}: HTTP {response.status_code}")
        if not response.content:
            raise LiveError(f"go2rtc returned an empty frame for {stream}")
        return response.content
    except LiveError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise LiveError(f"could not grab a frame from {stream}: {exc}") from exc
