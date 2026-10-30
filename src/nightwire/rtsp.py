"""RTSP URL credentials: strip them for storage, inject them at spawn,
redact them from anything a human or a log will see. Pure, stdlib only.

**Why this module exists.** ffmpeg and go2rtc can only authenticate to an
RTSP source by credentials embedded in the URL - there is no file or
environment option. The usual template is
`rtsp://USER:PASS@CAM_IP/...`, and the obvious implementation stores that
string in the `cameras.main_uri` column. That would put plaintext camera
passwords in the database, in every backup of it, in `/config/go2rtc.yaml`,
and in the UI wherever a stream URL is shown.

So: the database keeps a **credential-free** URI and the encrypted password
separately, the credentials are injected only into the argv of the process
that needs them, and `redact` scrubs them from ffmpeg's stderr before any of
it reaches the log or the Nodes page.

What this cannot fix: the credentials are visible in the spawned process's
`/proc/<pid>/cmdline`, because that is how ffmpeg takes them. Anyone with a
shell on the VM can read it. That is inherent and is documented rather than
pretended away.
"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit

# Matches the userinfo part of any URL: scheme://[user[:pass]@]host...
# Used for redaction, so it is deliberately broad about the scheme.
_USERINFO = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<userinfo>[^/@\s]+)@")

REDACTED = "***:***"


def split_userinfo(uri: str) -> tuple[str | None, str | None]:
    """The username and password embedded in a URI, if any.

    Percent-decoded, because this is how a pasted URI's credentials get
    harvested into the `username` column and the encrypted password, and
    `with_credentials` re-encodes them on the way back out. Storing the
    encoded form would double-encode on every spawn.
    """
    if not uri:
        return None, None
    try:
        parts = urlsplit(uri)
    except ValueError:
        return None, None
    username = unquote(parts.username) if parts.username else parts.username
    password = unquote(parts.password) if parts.password else parts.password
    return username, password


def has_credentials(uri: str) -> bool:
    username, password = split_userinfo(uri)
    return bool(username or password)


def strip_credentials(uri: str) -> str:
    """The URI with any embedded credentials removed.

    Used on the way into the database, so a URI pasted with credentials in it
    - which the design's manual template invites - does not store them in
    plaintext.
    """
    if not uri or "@" not in uri:
        return uri
    try:
        parts = urlsplit(uri)
    except ValueError:
        return uri
    if not (parts.username or parts.password):
        return uri

    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def with_credentials(uri: str, username: str | None, password: str | None) -> str:
    """The URI with these credentials embedded, replacing any already there.

    Percent-encodes both, because a camera password may legitimately contain
    `@`, `:` or `/`, and an unencoded one would silently produce a URL
    pointing somewhere else.
    """
    if not uri or not username:
        return strip_credentials(uri) if uri else uri
    try:
        parts = urlsplit(uri)
    except ValueError:
        return uri

    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    hostport = f"{host}:{parts.port}" if parts.port else host

    userinfo = quote(username, safe="")
    if password:
        userinfo += ":" + quote(password, safe="")
    return urlunsplit((parts.scheme, f"{userinfo}@{hostport}", parts.path, parts.query, parts.fragment))


def redact(text: str) -> str:
    """Replace the credentials in every URL inside a string.

    Applied to ffmpeg's stderr before it enters the per-camera ring buffer
    shown on Nodes, and installed as a logging filter, because a failed RTSP
    connection prints the whole URL.
    """
    if not text:
        return text
    return _USERINFO.sub(lambda m: f"{m.group('scheme')}{REDACTED}@", str(text))


def redact_argv(argv) -> list[str]:
    """A command line safe to log or show."""
    return [redact(part) for part in argv]
