"""Server-side sessions: issue, verify, revoke.

The storage half of `security.py`. One row per signed-in browser, keyed by
the SHA-256 of its cookie token, in the SQLite database on the /config
volume - so a container restart keeps every station tab signed in, which is
the property the original signed-timestamp design existed for, while revocation
works, which it did not.

What revokes a session:
  - **logout**, which deletes the row for that one token;
  - **a changed `NVR_PASSWORD` or `NVR_SECRET_KEY`**, through the
    `password_fp` column, checked on every request and swept at startup;
  - **expiry**, which slides on use so an open page stays signed in and an
    abandoned one does not.

Writes are deliberately rare. `verify` runs on every gated request,
including every HLS segment during playback, so it only writes when the row
has not been touched for `TOUCH_INTERVAL_SECONDS`. Without that, playing
back an hour of footage would mean thousands of UPDATEs on the same row.
"""

from __future__ import annotations

import hmac
import logging
import sqlite3
import time

from . import security

log = logging.getLogger("nightwire.sessions")

# How stale `last_seen_ms` is allowed to get before a request pays for an
# UPDATE. Also how coarse the sliding expiry is, which does not matter when
# the window is measured in days.
TOUCH_INTERVAL_SECONDS = 60

# Rows are small, but nothing should be able to grow a table without bound.
# Reaching this needs hundreds of distinct browsers signing in to a LAN-only
# NVR, so the oldest going early is the right trade.
MAX_SESSIONS = 500


def _now_ms(now_ms: int | None) -> int:
    return int(time.time() * 1000) if now_ms is None else int(now_ms)


def create(
    conn: sqlite3.Connection,
    *,
    password_fp: str,
    ttl_seconds: int,
    client: str = "",
    now_ms: int | None = None,
) -> str:
    """Issue a session. Returns the token, which is never stored."""
    now = _now_ms(now_ms)
    token = security.new_token()
    conn.execute(
        "INSERT INTO sessions (id, created_ms, expires_ms, last_seen_ms, password_fp, client)"
        " VALUES (?,?,?,?,?,?)",
        (security.token_id(token), now, now + ttl_seconds * 1000, now, password_fp, client or ""),
    )
    conn.commit()
    prune(conn, now_ms=now)
    return token


def verify(
    conn: sqlite3.Connection,
    token: str | None,
    *,
    password_fp: str,
    ttl_seconds: int,
    now_ms: int | None = None,
) -> sqlite3.Row | None:
    """The session row for this token, or None if it is not a valid session.

    A session whose `password_fp` no longer matches is deleted here rather
    than merely rejected: the password has changed, so it can never become
    valid again, and leaving it would need a second sweep later.
    """
    if not token:
        return None
    now = _now_ms(now_ms)
    row = conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (security.token_id(token),)
    ).fetchone()
    if row is None:
        return None

    if row["expires_ms"] <= now:
        conn.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))
        conn.commit()
        return None

    if not _same(row["password_fp"], password_fp):
        conn.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))
        conn.commit()
        log.info("session revoked: the password or secret key has changed since it was issued")
        return None

    if now - row["last_seen_ms"] >= TOUCH_INTERVAL_SECONDS * 1000:
        conn.execute(
            "UPDATE sessions SET last_seen_ms = ?, expires_ms = ? WHERE id = ?",
            (now, now + ttl_seconds * 1000, row["id"]),
        )
        conn.commit()
    return row


def _same(stored: str, current: str) -> bool:
    """Constant-time, because this is a comparison of secret-derived values."""
    return hmac.compare_digest(str(stored or ""), str(current or ""))


def revoke(conn: sqlite3.Connection, token: str | None) -> bool:
    """Sign one browser out. This is what makes logout mean something."""
    if not token:
        return False
    cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (security.token_id(token),))
    conn.commit()
    return cursor.rowcount > 0


def revoke_all(conn: sqlite3.Connection) -> int:
    """Sign every browser out."""
    cursor = conn.execute("DELETE FROM sessions")
    conn.commit()
    return cursor.rowcount


def revoke_stale_fingerprints(conn: sqlite3.Connection, password_fp: str) -> int:
    """Drop sessions issued under a different password or secret key.

    Run at startup so a password change takes effect even for a browser that
    never makes another request, and so the table does not keep rows that can
    never be valid again.
    """
    cursor = conn.execute("DELETE FROM sessions WHERE password_fp != ?", (password_fp,))
    conn.commit()
    if cursor.rowcount:
        log.info(
            "revoked %d session(s) issued under a previous password or secret key",
            cursor.rowcount,
        )
    return cursor.rowcount


def prune(conn: sqlite3.Connection, *, now_ms: int | None = None) -> int:
    """Delete expired sessions, and the oldest ones above `MAX_SESSIONS`."""
    now = _now_ms(now_ms)
    removed = conn.execute("DELETE FROM sessions WHERE expires_ms <= ?", (now,)).rowcount
    extra = conn.execute(
        "DELETE FROM sessions WHERE id IN ("
        "  SELECT id FROM sessions ORDER BY last_seen_ms DESC LIMIT -1 OFFSET ?"
        ")",
        (MAX_SESSIONS,),
    ).rowcount
    conn.commit()
    return removed + extra


def count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"])
