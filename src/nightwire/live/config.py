"""go2rtc configuration and stream names. Pure functions, stdlib only.

go2rtc carries live video only. Recording pulls RTSP straight from each
camera (see `recording/`), so if go2rtc crashes or restarts, recording is
unaffected. That separation is the reason this module never touches the
recorder's state.

Binding: the API and RTSP listeners stay on
localhost so the browser only ever talks to Nightwire's own port, while
WebRTC media needs a real port on the host (8555, TCP and UDP). With host
networking no ICE candidate configuration is required.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import cameras as camera_repo

API_HOST = "127.0.0.1"
API_PORT = 1984
RTSP_PORT = 8554
WEBRTC_PORT = 8555

MAIN_SUFFIX = "_main"
SUB_SUFFIX = "_sub"


def stream_name(camera_id: str, which: str) -> str:
    """`front` + `main` -> `front_main`. One name per camera per quality."""
    if which not in ("main", "sub"):
        raise ValueError(f"stream must be 'main' or 'sub', got {which!r}")
    return f"{camera_id}{MAIN_SUFFIX if which == 'main' else SUB_SUFFIX}"


def parse_stream_name(name: str) -> tuple[str, str] | None:
    """`front_sub` -> ('front', 'sub'). None if it is not one of ours."""
    for suffix, which in ((MAIN_SUFFIX, "main"), (SUB_SUFFIX, "sub")):
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)], which
    return None


@dataclass(frozen=True)
class StreamSpec:
    name: str
    source: str


def source_url(rtsp_uri: str) -> str:
    """The RTSP URL as go2rtc should consume it.

    `#backchannel=0` stops go2rtc negotiating two-way audio, which some
    cameras answer badly and which this app does not use at all.
    """
    if not rtsp_uri:
        return ""
    if "#backchannel" in rtsp_uri:
        return rtsp_uri
    return f"{rtsp_uri}#backchannel=0"


def streams_for(cameras, *, secret: str = "") -> list[StreamSpec]:
    """Two streams per camera: main for full-size, sub for grid tiles.

    A camera with no substream gets only a main stream, and the grid falls
    back to it. A camera with `record` off is still published: live view and
    recording are independent switches.

    `secret` is NVR_SECRET_KEY. With it, each source URL carries the camera's
    credentials, decrypted here, because go2rtc has no other way to
    authenticate to an RTSP source. Without it the URLs are credential-free,
    which is what the pure tests use and what an unauthenticated camera
    needs. The result therefore **contains secrets**: it goes into
    go2rtc.yaml at mode 0600 and into go2rtc's API over localhost, and
    nowhere else. Anything logged from it goes through `rtsp.redact`.
    """
    specs: list[StreamSpec] = []
    for camera in cameras:
        camera_id = camera["id"]
        keys = camera.keys()

        def uri(which: str) -> str:
            plain = (camera["main_uri"] if which == "main" else (camera["sub_uri"] if "sub_uri" in keys else None)) or ""
            if not plain or not secret:
                return plain
            return camera_repo.authenticated_uri(camera, secret, which) or plain

        main = source_url(uri("main"))
        if main:
            specs.append(StreamSpec(stream_name(camera_id, "main"), main))
        sub = source_url(uri("sub"))
        if sub:
            specs.append(StreamSpec(stream_name(camera_id, "sub"), sub))
    return specs


def grid_stream_for(camera: dict) -> str:
    """Which stream a grid tile should play: the substream when there is one."""
    camera_id = camera["id"]
    has_sub = bool(camera.get("sub_uri"))
    return stream_name(camera_id, "sub" if has_sub else "main")


def build_config(cameras, *, log_level: str = "warn", secret: str = "") -> dict:
    """The full go2rtc config document, written to /config/go2rtc.yaml.

    With a `secret` the stream sources carry camera credentials, so the file
    this produces must be written at mode 0600. See `streams_for`.
    """
    return {
        "api": {"listen": f"{API_HOST}:{API_PORT}"},
        "rtsp": {"listen": f"{API_HOST}:{RTSP_PORT}"},
        "webrtc": {"listen": f":{WEBRTC_PORT}"},
        "log": {"level": log_level},
        "streams": {spec.name: spec.source for spec in streams_for(cameras, secret=secret)},
    }


def _scalar(value) -> str:
    """A YAML double-quoted scalar.

    Everything is quoted, including numbers and the empty string: an RTSP URL
    routinely contains `?`, `&`, `@`, `:` and `#`, and `#` in particular
    starts a comment in plain style, which would silently truncate a source.
    """
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def to_yaml(document: dict) -> str:
    """Serialise the config without depending on PyYAML.

    **Block style, not JSON.** JSON is valid YAML 1.2 and go2rtc reads a JSON
    document at startup perfectly well, which is why this emitted JSON until
    2026-10-30. But go2rtc **edits this file itself** when a stream is added
    through its API, by patching the YAML text - and its patcher cannot
    handle a flow-style document. It answered every stream registration with
    `400 yaml: line 10: did not find expected ',' or '}'`, the supervisor
    treated that as a crash, and go2rtc was restarted every two seconds
    forever. Found the first time the UI was opened in a browser.

    The structure here is fixed and shallow - two levels, string scalars - so
    this stays a dozen lines rather than a PyYAML dependency.
    """
    lines: list[str] = []
    for key in sorted(document):
        value = document[key]
        if isinstance(value, dict):
            lines.append(f"{key}:")
            if not value:
                lines[-1] = f"{key}: {{}}"
                continue
            for inner in sorted(value):
                lines.append(f"  {inner}: {_scalar(value[inner])}")
        else:
            lines.append(f"{key}: {_scalar(value)}")
    return "\n".join(lines) + "\n"


def api_url(path: str = "") -> str:
    base = f"http://{API_HOST}:{API_PORT}"
    if not path:
        return base
    return f"{base}/{path.lstrip('/')}"


def ws_url(stream: str) -> str:
    """go2rtc's signalling socket for one stream, on localhost."""
    return f"ws://{API_HOST}:{API_PORT}/api/ws?src={stream}"


def frame_url(stream: str) -> str:
    """go2rtc's still-frame endpoint. Preferred over an ONVIF snapshot."""
    return api_url(f"api/frame.jpeg?src={stream}")
