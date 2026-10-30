"""ONVIF access, split in two so the hard dependency stays in one file.

- `parse` is pure: plain dicts in, normalised dataclasses out. Testable with
  no ONVIF stack installed, and where the profile-selection logic lives.
- `client` is the only module that imports `onvif-zeep-async` or
  `WSDiscovery`, both lazily, and the only one that touches the network.
"""

from .parse import DeviceInfo, ProbeResult, StreamProfile  # noqa: F401
