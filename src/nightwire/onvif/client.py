"""The only module that talks ONVIF.

Everything the rest of the app needs from a camera goes through here, so the
dependency on `onvif-zeep-async` and `WSDiscovery` is contained to one file
and the interpretation of what comes back lives in `parse.py`.

Both libraries are imported lazily. If they are missing the app still starts
and `/api/health` still answers; only discovery and probing fail, with an
error that says what to install rather than a 500.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
from pathlib import Path

from . import parse
from .parse import DeviceInfo, ProbeResult

log = logging.getLogger("nightwire.onvif")

DISCOVERY_SECONDS = 5.0
PROBE_TIMEOUT_SECONDS = 10.0


class OnvifUnavailable(RuntimeError):
    """The ONVIF client libraries are not installed."""


class OnvifError(RuntimeError):
    """The camera refused, timed out, or answered something unusable."""


def _require_onvif():
    try:
        from onvif import ONVIFCamera
    except ModuleNotFoundError as exc:
        raise OnvifUnavailable(
            "ONVIF support needs `onvif-zeep-async`. Install it with "
            "`pip install -e .` or rebuild the container image."
        ) from exc
    return ONVIFCamera


# zeep caches parsed WSDL documents in a SQLite file under the user's cache
# directory. Inside the container the process runs as `nvr` (uid 1000) after
# the entrypoint drops privileges, while HOME is still root's, so the default
# location is unwritable and **every ONVIF call fails** with
# "[Errno 13] Permission denied: '/root/.cache'" - before any question of
# camera credentials. The image now sets HOME and XDG_CACHE_HOME to a
# writable directory; this check is the second layer, so a deployment that
# gets that wrong loses the cache's speed rather than ONVIF entirely.
_cache_usable: bool | None = None


def cache_dir() -> Path:
    """Where zeep would put its WSDL cache, by the rules zeep itself uses."""
    override = os.environ.get("XDG_CACHE_HOME")
    base = Path(override) if override else Path(os.path.expanduser("~")) / ".cache"
    return base / "zeep"


def cache_is_writable(directory: Path | None = None) -> bool:
    """Whether a file can actually be created there, not merely stat'ed.

    `os.access` lies under some container filesystems and says nothing about
    a directory that does not exist yet, so this creates and removes a file.
    """
    target = cache_dir() if directory is None else Path(directory)
    try:
        target.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target):
            return True
    except OSError:
        return False


def _no_cache() -> bool:
    """True when zeep must not try to write its cache. Checked once."""
    global _cache_usable
    if _cache_usable is None:
        _cache_usable = cache_is_writable()
        if not _cache_usable:
            log.warning(
                "%s is not writable, so ONVIF runs without its WSDL cache. "
                "Calls still work and are a little slower on first use.",
                cache_dir(),
            )
    return not _cache_usable


def build_camera(host: str, port: int, username: str | None, password: str | None):
    """Build an ONVIFCamera. The one place one is constructed.

    `adjust_time=True` is **not optional**. ONVIF authenticates with a
    WS-Security UsernameToken whose digest covers a timestamp, and a camera
    rejects a token whose clock is too far from its own - with
    "Sender not authorized", which is indistinguishable from a wrong
    password. A camera rejected a correct user and password for exactly
    this reason; with time adjustment the same credentials worked first try. The library reads the camera's clock from
    the unauthenticated GetSystemDateAndTime and signs against that.

    This is also why `events/clock.py` exists: the same skew puts a camera's
    event timestamps in the wrong place on the timeline.
    """
    return _require_onvif()(
        host,
        int(port),
        username or "",
        password or "",
        no_cache=_no_cache(),
        adjust_time=True,
    )


def _explain(host: str, port: int, exc: Exception) -> str:
    """Turn a camera's refusal into something actionable.

    "Sender not authorized" is a common answer when the ONVIF
    user or password is wrong or missing, and on its own it reads like a
    problem with Nightwire rather than with the credentials typed in.
    """
    text = str(exc)
    lowered = text.lower()
    if "not authorized" in lowered or "unauthorized" in lowered or "401" in lowered:
        return (
            f"{host}:{port} rejected those credentials. Check the ONVIF user and "
            "password, and that the user has ONVIF rights on the camera "
            f"(the camera said: {text})."
        )
    return f"{host}:{port} refused ONVIF: {text}"


def _require_discovery():
    try:
        from wsdiscovery.discovery import ThreadedWSDiscovery
        from wsdiscovery import QName
    except ModuleNotFoundError as exc:
        raise OnvifUnavailable(
            "Network discovery needs `WSDiscovery`. Install it with "
            "`pip install -e .` or rebuild the container image. Adding a "
            "camera by IP address works without it."
        ) from exc
    return ThreadedWSDiscovery, QName


def available() -> bool:
    try:
        import onvif  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


def discovery_available() -> bool:
    try:
        import wsdiscovery  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


def _as_dict(value) -> dict:
    """zeep returns objects with attribute access; flatten to plain dicts.

    Recursive, because a media profile nests VideoEncoderConfiguration ->
    Resolution. Anything not object-like passes through untouched so parse.py
    only ever sees dicts, lists and scalars.
    """
    if value is None:
        return {}
    if _is_element(value):
        return element_to_dict(value)
    if isinstance(value, dict):
        items = value.items()
    elif hasattr(value, "__values__"):  # zeep CompoundValue
        items = value.__values__.items()
    else:
        return {}

    converted: dict = {}
    for key, item in items:
        # zeep parks xsd:any content under `_value_1` as raw elements. A
        # TopicSet's entire tree lives there, so each element is lifted into
        # the parent **under its own tag name**, which is the thing
        # `element_to_dict` cannot know about itself.
        if key == "_value_1" and isinstance(item, (list, tuple)):
            for element in item:
                if _is_element(element):
                    converted.setdefault(local_name(element.tag), element_to_dict(element))
                else:
                    converted.setdefault(key, _flatten(item))
            continue
        converted[key] = _flatten(item)
    return converted


def _flatten(value):
    if isinstance(value, (list, tuple)):
        return [_flatten(v) for v in value]
    if _is_element(value):
        return element_to_dict(value)
    if isinstance(value, dict) or hasattr(value, "__values__"):
        return _as_dict(value)
    return value


def _is_element(value) -> bool:
    """An XML element, without importing lxml to find out.

    Duck-typed on the three attributes both lxml and the standard library's
    ElementTree carry, so the conversion is testable with stdlib XML.
    """
    return hasattr(value, "tag") and hasattr(value, "attrib") and hasattr(value, "find")


def local_name(tag) -> str:
    """`{http://www.onvif.org/ver10/topics}VideoSource` -> `VideoSource`."""
    text = str(tag)
    return text.rsplit("}", 1)[-1] if "}" in text else text


def element_to_dict(element, depth: int = 0) -> dict:
    """An lxml element as the nested dicts the rest of the app expects.

    **GetEventProperties returns its TopicSet as xsd:any**, which zeep hands
    back as a list of raw lxml elements under `_value_1` rather than as a
    parsed structure. `events/topics.flatten` walks dicts and skips keys
    starting with an underscore, so on a real camera it found **zero event
    sources** - no motion events, ever. The camera that found this advertises
    ten topic branches and Nightwire saw none of them.

    Namespace prefixes are dropped: a topic path is `VideoSource/MotionAlarm`
    whether the camera puts it in the ONVIF or a vendor namespace, and the
    rest of the app matches on that path.
    """
    if depth > 24:
        return {}
    out: dict = {}
    attributes = {local_name(k): v for k, v in dict(element.attrib).items()}
    if attributes:
        out["_attr_1"] = attributes
        # Attributes are also exposed by name, because `_names` reads
        # `Name` straight off a SimpleItemDescription.
        for key, value in attributes.items():
            out.setdefault(key, value)

    for child in element:
        if not _is_element(child):
            continue
        key = local_name(child.tag)
        converted = element_to_dict(child, depth + 1)
        if key in out and not isinstance(out[key], list):
            out[key] = [out[key], converted]
        elif key in out:
            out[key].append(converted)
        else:
            out[key] = converted

    text = (element.text or "").strip()
    if text and not out:
        return {"_value_1": text}
    return out


async def _discover_blocking(timeout: float) -> list[dict]:
    """WS-Discovery is synchronous and multicast; run it off the event loop."""
    ThreadedWSDiscovery, QName = _require_discovery()

    def run() -> list[dict]:
        service = ThreadedWSDiscovery()
        service.start()
        try:
            # NetworkVideoTransmitter is the ONVIF device type for a camera.
            scope = QName("http://www.onvif.org/ver10/network/wsdl", "NetworkVideoTransmitter")
            services = service.searchServices(types=[scope], timeout=timeout)
            found = []
            for item in services:
                addresses = " ".join(item.getXAddrs() or [])
                host, port = parse.parse_discovery_address(addresses)
                if not host:
                    continue
                scopes = [str(s.getValue()) for s in (item.getScopes() or [])]
                found.append(
                    {
                        "host": host,
                        "port": port,
                        "xaddrs": addresses,
                        "scopes": scopes,
                        # Many devices publish name and hardware in scopes.
                        "name": _scope_value(scopes, "name"),
                        "hardware": _scope_value(scopes, "hardware"),
                    }
                )
            return found
        finally:
            service.stop()

    return await asyncio.to_thread(run)


def _scope_value(scopes: list[str], key: str) -> str:
    marker = f"onvif://www.onvif.org/{key}/"
    for scope in scopes:
        if scope.startswith(marker):
            return scope[len(marker) :].replace("%20", " ")
    return ""


async def discover(timeout: float = DISCOVERY_SECONDS) -> list[dict]:
    """Run WS-Discovery and return the cameras that answered.

    Only finds cameras on the same layer-2 segment, which is why adding by IP
    must always work. Cameras on a flat untagged LAN with the host are seen;
    cameras behind a VLAN or a router are not.
    """
    try:
        found = await _discover_blocking(timeout)
    except OnvifUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - multicast fails in many ways
        raise OnvifError(f"Discovery failed: {exc}") from exc
    log.info("discovery found %d device(s)", len(found))
    return found


async def probe(host: str, port: int, username: str | None, password: str | None) -> ProbeResult:
    """Read device info and media profiles from one camera.

    This is what the Nodes page calls before saving a camera, and what "Test
    connection" calls afterwards.
    """
    camera = None
    try:
        camera = build_camera(host, port, username, password)
        await asyncio.wait_for(camera.update_xaddrs(), timeout=PROBE_TIMEOUT_SECONDS)

        device_service = await camera.create_devicemgmt_service()
        raw_info = await asyncio.wait_for(
            device_service.GetDeviceInformation(), timeout=PROBE_TIMEOUT_SECONDS
        )
        device = parse.normalise_device_info(_as_dict(raw_info))

        media = await camera.create_media_service()
        raw_profiles = await asyncio.wait_for(media.GetProfiles(), timeout=PROBE_TIMEOUT_SECONDS)
        profiles = parse.normalise_profiles([_as_dict(p) for p in (raw_profiles or [])])
        main, sub = parse.choose_main_sub(profiles)

        return ProbeResult(
            host=host,
            port=port,
            device=device,
            profiles=profiles,
            suggested_main=main.token if main else None,
            suggested_sub=sub.token if sub else None,
            event_source_count=await _count_event_sources(camera),
        )
    except OnvifUnavailable:
        raise
    except asyncio.TimeoutError as exc:
        raise OnvifError(
            f"{host}:{port} did not answer ONVIF within {PROBE_TIMEOUT_SECONDS:.0f}s. "
            "Check the address, the ONVIF port, and that an ONVIF user exists."
        ) from exc
    except Exception as exc:  # noqa: BLE001 - zeep raises a wide variety
        raise OnvifError(_explain(host, port, exc)) from exc
    finally:
        if camera is not None:
            try:
                await camera.close()
            except Exception:  # noqa: BLE001 - closing must never mask the real error
                pass


async def _count_event_sources(camera) -> int | None:
    """How many event topics the camera advertises.

    Counted at probe time so the Nodes page can say "5 motion event sources
    found, choose them after saving". The sources themselves are read through
    GetEventProperties, with no topic string hard-coded anywhere.
    """
    try:
        events = await camera.create_events_service()
        properties = await asyncio.wait_for(
            events.GetEventProperties(), timeout=PROBE_TIMEOUT_SECONDS
        )
        topics = _as_dict(properties).get("TopicSet") or {}
        return _count_leaves(topics)
    except Exception as exc:  # noqa: BLE001 - an optional nicety, never fatal
        log.debug("event source count unavailable: %s", exc)
        return None


def _count_leaves(node, depth: int = 0) -> int:
    """Count topic leaves in an ONVIF TopicSet tree."""
    if depth > 8 or not isinstance(node, dict):
        return 0
    total = 0
    for key, value in node.items():
        if key.startswith("_"):
            continue
        if isinstance(value, dict):
            nested = _count_leaves(value, depth + 1)
            total += nested if nested else 1
    return total


async def stream_uri(
    host: str, port: int, username: str | None, password: str | None, profile_token: str
) -> str:
    """Ask the camera for a profile's RTSP URI.

    Preferred over building a URL by hand. The manual templates in the spec
    are the fallback for a camera that will not answer this.
    """
    camera = None
    try:
        camera = build_camera(host, port, username, password)
        await asyncio.wait_for(camera.update_xaddrs(), timeout=PROBE_TIMEOUT_SECONDS)
        media = await camera.create_media_service()
        request = media.create_type("GetStreamUri")
        request.ProfileToken = profile_token
        request.StreamSetup = {
            "Stream": "RTP-Unicast",
            "Transport": {"Protocol": "RTSP"},
        }
        response = await asyncio.wait_for(
            media.GetStreamUri(request), timeout=PROBE_TIMEOUT_SECONDS
        )
        uri = _as_dict(response).get("Uri") or ""
        if not uri:
            raise OnvifError(f"{host} returned no stream URI for profile {profile_token}")
        return str(uri)
    except (OnvifUnavailable, OnvifError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise OnvifError(f"GetStreamUri failed on {host}: {exc}") from exc
    finally:
        if camera is not None:
            try:
                await camera.close()
            except Exception:  # noqa: BLE001
                pass


async def snapshot_uri(
    host: str, port: int, username: str | None, password: str | None, profile_token: str
) -> str:
    camera = None
    try:
        camera = build_camera(host, port, username, password)
        await asyncio.wait_for(camera.update_xaddrs(), timeout=PROBE_TIMEOUT_SECONDS)
        media = await camera.create_media_service()
        request = media.create_type("GetSnapshotUri")
        request.ProfileToken = profile_token
        response = await asyncio.wait_for(
            media.GetSnapshotUri(request), timeout=PROBE_TIMEOUT_SECONDS
        )
        uri = _as_dict(response).get("Uri") or ""
        if not uri:
            raise OnvifError(f"{host} returned no snapshot URI")
        return str(uri)
    except (OnvifUnavailable, OnvifError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise OnvifError(f"GetSnapshotUri failed on {host}: {exc}") from exc
    finally:
        if camera is not None:
            try:
                await camera.close()
            except Exception:  # noqa: BLE001
                pass


async def event_properties(host: str, port: int, username: str | None, password: str | None) -> dict:
    """The camera's TopicSet, flattened to plain dicts.

    `events/topics.py` turns this into the choosable list on Nodes. No topic
    string is interpreted here or there; the camera is asked what it offers.
    """
    camera = None
    try:
        camera = build_camera(host, port, username, password)
        await asyncio.wait_for(camera.update_xaddrs(), timeout=PROBE_TIMEOUT_SECONDS)
        events = await camera.create_events_service()
        properties = await asyncio.wait_for(
            events.GetEventProperties(), timeout=PROBE_TIMEOUT_SECONDS
        )
        return _as_dict(properties).get("TopicSet") or {}
    except OnvifUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OnvifError(f"GetEventProperties failed on {host}: {exc}") from exc
    finally:
        if camera is not None:
            try:
                await camera.close()
            except Exception:  # noqa: BLE001
                pass


async def system_time_ms(
    host: str, port: int, username: str | None, password: str | None
) -> tuple[int, int, int]:
    """The camera's UTC clock, with the request and response times around it.

    Returns `(camera_utc_ms, sent_at_ms, received_at_ms)` so
    `events/clock.measure` can split the network latency rather than counting
    all of it as skew.
    """
    import time

    from ..events.clock import parse_utc

    camera = None
    try:
        camera = build_camera(host, port, username, password)
        await asyncio.wait_for(camera.update_xaddrs(), timeout=PROBE_TIMEOUT_SECONDS)
        device_service = await camera.create_devicemgmt_service()
        sent_at = int(time.time() * 1000)
        response = await asyncio.wait_for(
            device_service.GetSystemDateAndTime(), timeout=PROBE_TIMEOUT_SECONDS
        )
        received_at = int(time.time() * 1000)
        document = _as_dict(response)
        camera_ms = parse_utc(document.get("UTCDateTime"))
        if camera_ms is None:
            raise OnvifError(f"{host} returned no usable UTC time")
        return camera_ms, sent_at, received_at
    except (OnvifUnavailable, OnvifError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise OnvifError(f"GetSystemDateAndTime failed on {host}: {exc}") from exc
    finally:
        if camera is not None:
            try:
                await camera.close()
            except Exception:  # noqa: BLE001
                pass


class PullPoint:
    """One PullPoint subscription, wrapped so the listener stays readable.

    Opened with `connect`, read with `pull`, kept alive with `renew`. The
    listener owns the backoff and the lifecycle; this only speaks ONVIF.
    """

    def __init__(self, host: str, port: int, username: str | None, password: str | None) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self._camera = None
        self._service = None
        self.termination_seconds = 600

    async def connect(self, topics: list[str] | None = None) -> None:
        self._camera = build_camera(self.host, self.port, self.username, self.password)
        await asyncio.wait_for(self._camera.update_xaddrs(), timeout=PROBE_TIMEOUT_SECONDS)
        # The subscription is filtered to the chosen topics where the camera
        # supports it; an unfiltered subscription still works, just noisier.
        await self._camera.create_pullpoint_subscription()
        self._service = await self._camera.create_pullpoint_service()

    async def pull(self, timeout_seconds: int = 10, limit: int = 50) -> list[dict]:
        if self._service is None:
            raise OnvifError("pull before connect")
        request = self._service.create_type("PullMessages")
        request.Timeout = f"PT{int(timeout_seconds)}S"
        request.MessageLimit = int(limit)
        response = await asyncio.wait_for(
            self._service.PullMessages(request), timeout=timeout_seconds + 10
        )
        document = _as_dict(response)
        messages = document.get("NotificationMessage") or []
        if isinstance(messages, dict):
            messages = [messages]
        return [m if isinstance(m, dict) else {} for m in messages]

    async def renew(self) -> None:
        if self._service is None:
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                self._service.SetSynchronizationPoint(), timeout=PROBE_TIMEOUT_SECONDS
            )

    async def close(self) -> None:
        if self._camera is not None:
            try:
                await self._camera.close()
            except Exception:  # noqa: BLE001
                pass
        self._camera = None
        self._service = None

