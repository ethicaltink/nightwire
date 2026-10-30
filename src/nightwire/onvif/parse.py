"""Pure interpretation of ONVIF responses.

Everything in here works on plain dicts, never on zeep objects, so the
decision logic is testable without the ONVIF stack installed. `client.py`
does the talking and hands its results through these functions.

Nothing here hard-codes a vendor's topic string or model name. The camera is
asked what it offers and this module ranks the answers.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

# Preferred codecs, best first. H.264 wins for v1: every browser decodes it in
# both hls.js and WebRTC, while H.265 depends on browser and hardware.
CODEC_RANK = {"H264": 0, "H265": 1, "HEVC": 1, "MPEG4": 2, "JPEG": 3}

# Names the spec asks the operator to create on the camera. An exact match
# beats any heuristic, because it is an explicit statement of intent.
MAIN_NAME_HINTS = ("nvr-main",)
SUB_NAME_HINTS = ("nvr-sub",)


@dataclass(frozen=True)
class StreamProfile:
    token: str
    name: str
    codec: str = ""
    width: int = 0
    height: int = 0
    fps: int = 0
    bitrate_kbps: int = 0

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def summary(self) -> str:
        """The one-line form the Nodes page shows, e.g.
        'nvr-main - 1920x1080 - 15 fps - H.264'."""
        parts = [self.name]
        if self.width and self.height:
            parts.append(f"{self.width}x{self.height}")
        if self.fps:
            parts.append(f"{self.fps} fps")
        if self.codec:
            parts.append(pretty_codec(self.codec))
        return " · ".join(parts)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["summary"] = self.summary
        data["pixels"] = self.pixels
        return data


@dataclass(frozen=True)
class DeviceInfo:
    manufacturer: str = ""
    model: str = ""
    firmware: str = ""
    serial: str = ""
    hardware: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProbeResult:
    host: str
    port: int
    device: DeviceInfo
    profiles: list[StreamProfile] = field(default_factory=list)
    suggested_main: str | None = None
    suggested_sub: str | None = None
    event_source_count: int | None = None

    def as_dict(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "device": self.device.as_dict(),
            "profiles": [p.as_dict() for p in self.profiles],
            "suggested_main": self.suggested_main,
            "suggested_sub": self.suggested_sub,
            "event_source_count": self.event_source_count,
        }


def pretty_codec(codec: str) -> str:
    mapping = {"H264": "H.264", "H265": "H.265", "HEVC": "H.265", "JPEG": "MJPEG"}
    return mapping.get((codec or "").upper(), codec or "")


def _int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


# Some cameras report `BitrateLimit` as 2147483647 - INT_MAX, meaning
# "not capped" - and showing "2147483647 kbps" in the profile picker is worse
# than showing nothing. Anything at or above this is treated as unset.
UNCAPPED_BITRATE = 2_000_000


def _bitrate(value) -> int:
    number = _int(value)
    return 0 if number >= UNCAPPED_BITRATE else number


def normalise_profile(raw: dict) -> StreamProfile:
    """Flatten one ONVIF media profile into a StreamProfile.

    Tolerates the shapes real firmware returns: the video encoder
    configuration may be absent entirely (audio-only or metadata profiles),
    and rate control may be missing on a fixed-rate profile.
    """
    encoder = raw.get("VideoEncoderConfiguration") or {}
    resolution = encoder.get("Resolution") or {}
    rate = encoder.get("RateControl") or {}
    return StreamProfile(
        token=str(raw.get("token") or raw.get("Token") or ""),
        name=str(raw.get("Name") or raw.get("name") or ""),
        codec=str(encoder.get("Encoding") or ""),
        width=_int(resolution.get("Width")),
        height=_int(resolution.get("Height")),
        fps=_int(rate.get("FrameRateLimit")),
        bitrate_kbps=_bitrate(rate.get("BitrateLimit")),
    )


def normalise_profiles(raw_profiles) -> list[StreamProfile]:
    """Normalise and drop anything that cannot carry video."""
    profiles = [normalise_profile(raw) for raw in (raw_profiles or [])]
    return [p for p in profiles if p.token and p.pixels]


def _codec_rank(profile: StreamProfile) -> int:
    return CODEC_RANK.get((profile.codec or "").upper(), 9)


def _by_name(profiles: list[StreamProfile], hints) -> StreamProfile | None:
    for hint in hints:
        for profile in profiles:
            if profile.name.strip().lower() == hint:
                return profile
    return None


def choose_main_sub(
    profiles: list[StreamProfile],
) -> tuple[StreamProfile | None, StreamProfile | None]:
    """Suggest which profile records and which feeds the grid.

    A profile literally named nvr-main or nvr-sub wins, because the design's
    camera prep checklist asks the operator to create exactly those. Failing
    that: main is the largest frame in the most playable codec, and sub is the
    smallest playable frame that is not the main one.

    These are suggestions. The Nodes page always lets the operator override.
    """
    if not profiles:
        return None, None

    playable = [p for p in profiles if _codec_rank(p) < 3] or list(profiles)

    main = _by_name(profiles, MAIN_NAME_HINTS)
    if main is None:
        main = sorted(playable, key=lambda p: (_codec_rank(p), -p.pixels, p.name))[0]

    sub = _by_name(profiles, SUB_NAME_HINTS)
    if sub is None:
        smaller = [p for p in playable if p.token != main.token and p.pixels < main.pixels]
        pool = smaller or [p for p in playable if p.token != main.token]
        sub = sorted(pool, key=lambda p: (_codec_rank(p), p.pixels, p.name))[0] if pool else None

    return main, sub


def normalise_device_info(raw: dict) -> DeviceInfo:
    return DeviceInfo(
        manufacturer=str(raw.get("Manufacturer") or ""),
        model=str(raw.get("Model") or ""),
        firmware=str(raw.get("FirmwareVersion") or ""),
        serial=str(raw.get("SerialNumber") or ""),
        hardware=str(raw.get("HardwareId") or ""),
    )


def parse_discovery_address(xaddr: str) -> tuple[str, int]:
    """Pull host and port out of a WS-Discovery XAddr.

    XAddrs look like 'http://192.168.1.100/onvif/device_service', sometimes with
    an explicit port, occasionally with several addresses space-separated.
    """
    first = (xaddr or "").split()[0] if (xaddr or "").strip() else ""
    if not first:
        return "", 80
    without_scheme = first.split("://", 1)[-1]
    authority = without_scheme.split("/", 1)[0]
    if authority.startswith("["):  # IPv6 literal
        host, _, rest = authority.partition("]")
        host = host.lstrip("[")
        port = rest.lstrip(":")
        return host, _int(port) or 80
    if ":" in authority:
        host, _, port = authority.partition(":")
        return host, _int(port) or 80
    return authority, 80


def slug_from_name(name: str, taken=()) -> str:
    """A short lowercase id from a display name, e.g. 'Front Door' -> 'front'.

    Camera ids appear in file paths under /recordings and in URLs, so they
    stay to lowercase letters, digits and hyphens.
    """
    cleaned = []
    for char in (name or "").strip().lower():
        if char.isalnum():
            cleaned.append(char)
        elif cleaned and cleaned[-1] != "-":
            cleaned.append("-")
    base = "".join(cleaned).strip("-") or "camera"
    # 'Front Door' -> 'front', but keep multi-word names that are short enough.
    first = base.split("-")[0]
    candidate = first if len(first) >= 3 else base
    taken = set(taken)
    if candidate not in taken:
        return candidate
    if base not in taken:
        return base
    index = 2
    while f"{candidate}-{index}" in taken:
        index += 1
    return f"{candidate}-{index}"
