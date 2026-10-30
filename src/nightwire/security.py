"""Session tokens and password checking. Standard library only.

**A session was an HMAC-signed issue time until 2026-10-29**, with no
database row, so that a container restart would not sign the viewing station
out. That is why none of them could be revoked: logout cleared the cookie in
the browser and its signature stayed valid, and changing NVR_PASSWORD left
every issued session working. Finding 4 of the security review.

A session is now a random token whose SHA-256 is a row in `sessions`
(`sessions.py`), which keeps the restart property - the database is on the
/config volume - and gets revocation back. This module stays pure and
stdlib-only: it mints and fingerprints, and `sessions.py` does the storage.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

COOKIE_NAME = "nightwire_session"

# 32 bytes from the OS. A guessing attack on this is not the threat model,
# but the token is also the only thing standing between the LAN and 45 days
# of footage, so there is no reason to be clever about the width.
TOKEN_BYTES = 32


def check_password(expected: str, given: str) -> bool:
    """Constant-time password comparison."""
    return hmac.compare_digest(expected.encode(), (given or "").encode())


def new_token() -> str:
    """A fresh session token. This value goes in the cookie and nowhere else."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_id(token: str) -> str:
    """The stored form of a token.

    Only the hash is written to the database, so a readable copy of the
    `sessions` table is not a set of usable cookies. A plain SHA-256 and not
    a password hash on purpose: the input is 256 bits of OS randomness, so
    there is nothing to brute-force and a slow KDF would only make every
    request slower.
    """
    return hashlib.sha256((token or "").encode()).hexdigest()


def password_fingerprint(secret: str, password: str) -> str:
    """An identifier for the password a session was issued under.

    Stored per session so that changing NVR_PASSWORD - or rotating
    NVR_SECRET_KEY, which keys this HMAC - invalidates sessions issued
    before the change, without storing either value.
    """
    return hmac.new(secret.encode(), (password or "").encode(), hashlib.sha256).hexdigest()


def needs_refresh(created_ms: int, refresh_after_seconds: int, now_ms: int | None = None) -> bool:
    """Whether the browser's copy of the cookie should be re-sent.

    The server-side expiry slides on use, but the cookie's own `max-age` was
    fixed when it was set, so a tab left open for weeks needs it refreshed.
    """
    current = int(time.time() * 1000 if now_ms is None else now_ms)
    return (current - created_ms) >= refresh_after_seconds * 1000
