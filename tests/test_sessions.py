"""Server-side sessions: the revocation that finding 4 was about.

A session used to be an HMAC-signed timestamp with no stored state, so
nothing could revoke one. These tests pin the three things that now can:
logout, a changed password or secret key, and expiry.
"""

from __future__ import annotations

import sqlite3

from nightwire import db, security, sessions

SECRET = "s" * 40
PASSWORD = "hunter2hunter2"
DAY = 86400
FP = security.password_fingerprint(SECRET, PASSWORD)


def fresh(tmp_path) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "nightwire.db")
    db.migrate(conn)
    return conn


def issue(conn, *, now_ms=0, ttl=30 * DAY, fp=FP, client="10.0.0.9"):
    return sessions.create(
        conn, password_fp=fp, ttl_seconds=ttl, client=client, now_ms=now_ms
    )


def check(conn, token, *, now_ms=0, ttl=30 * DAY, fp=FP):
    return sessions.verify(conn, token, password_fp=fp, ttl_seconds=ttl, now_ms=now_ms)


def test_a_fresh_session_verifies(tmp_path):
    conn = fresh(tmp_path)
    token = issue(conn)
    row = check(conn, token)
    assert row is not None
    assert row["client"] == "10.0.0.9"


def test_the_token_itself_is_never_stored(tmp_path):
    """A readable copy of this table must not be a set of usable cookies."""
    conn = fresh(tmp_path)
    token = issue(conn)
    [row] = list(conn.execute("SELECT * FROM sessions"))
    assert row["id"] != token
    assert token not in str(tuple(row))


def test_the_password_is_not_in_the_table(tmp_path):
    conn = fresh(tmp_path)
    issue(conn)
    [row] = list(conn.execute("SELECT * FROM sessions"))
    assert PASSWORD not in str(tuple(row))
    assert SECRET not in str(tuple(row))


def test_an_unknown_token_is_not_a_session(tmp_path):
    conn = fresh(tmp_path)
    issue(conn)
    assert check(conn, security.new_token()) is None


def test_no_token_is_not_a_session(tmp_path):
    conn = fresh(tmp_path)
    assert check(conn, None) is None
    assert check(conn, "") is None


# ------------------------------------------------------------------- logout


def test_logout_revokes_the_session(tmp_path):
    conn = fresh(tmp_path)
    token = issue(conn)
    assert sessions.revoke(conn, token) is True
    assert check(conn, token) is None
    assert sessions.count(conn) == 0


def test_revoking_one_session_leaves_the_others(tmp_path):
    """The station's tabs must not be signed out by a laptop logging out."""
    conn = fresh(tmp_path)
    station = issue(conn, client="10.0.0.20")
    laptop = issue(conn, client="10.0.0.21")
    sessions.revoke(conn, laptop)
    assert check(conn, station) is not None
    assert check(conn, laptop) is None


def test_revoking_an_unknown_token_is_not_an_error(tmp_path):
    conn = fresh(tmp_path)
    assert sessions.revoke(conn, security.new_token()) is False
    assert sessions.revoke(conn, None) is False


def test_revoke_all(tmp_path):
    conn = fresh(tmp_path)
    tokens = [issue(conn) for _ in range(3)]
    assert sessions.revoke_all(conn) == 3
    assert all(check(conn, token) is None for token in tokens)


# -------------------------------------------- a changed password or secret


def test_a_changed_password_invalidates_a_session(tmp_path):
    conn = fresh(tmp_path)
    token = issue(conn)
    changed = security.password_fingerprint(SECRET, "different-password")
    assert check(conn, token, fp=changed) is None


def test_an_invalidated_session_is_deleted_not_merely_refused(tmp_path):
    """It can never become valid again, so it should not linger."""
    conn = fresh(tmp_path)
    token = issue(conn)
    check(conn, token, fp=security.password_fingerprint(SECRET, "different-password"))
    assert sessions.count(conn) == 0


def test_a_rotated_secret_key_invalidates_a_session(tmp_path):
    conn = fresh(tmp_path)
    token = issue(conn)
    rotated = security.password_fingerprint("other" * 10, PASSWORD)
    assert check(conn, token, fp=rotated) is None


def test_the_startup_sweep_drops_sessions_from_a_previous_password(tmp_path):
    conn = fresh(tmp_path)
    old = issue(conn, fp=security.password_fingerprint(SECRET, "old-password-here"))
    current = issue(conn)
    assert sessions.revoke_stale_fingerprints(conn, FP) == 1
    assert check(conn, current) is not None
    assert check(conn, old) is None


# ------------------------------------------------------------------- expiry


def test_a_session_expires(tmp_path):
    conn = fresh(tmp_path)
    token = issue(conn, now_ms=0, ttl=DAY)
    assert check(conn, token, now_ms=DAY * 1000 + 1, ttl=DAY) is None


def test_an_expired_session_is_deleted(tmp_path):
    conn = fresh(tmp_path)
    token = issue(conn, now_ms=0, ttl=DAY)
    check(conn, token, now_ms=DAY * 1000 + 1, ttl=DAY)
    assert sessions.count(conn) == 0


def test_expiry_slides_on_use_so_an_open_page_stays_signed_in(tmp_path):
    """The viewing station is left open for weeks; that is the point."""
    conn = fresh(tmp_path)
    token = issue(conn, now_ms=0, ttl=DAY)
    now = 0
    for _ in range(10):
        now += int(DAY * 1000 * 0.9)
        assert check(conn, token, now_ms=now, ttl=DAY) is not None
    assert now > DAY * 1000 * 5


def test_an_idle_session_does_not_slide(tmp_path):
    conn = fresh(tmp_path)
    token = issue(conn, now_ms=0, ttl=DAY)
    assert check(conn, token, now_ms=int(DAY * 1000 * 0.5), ttl=DAY) is not None
    assert check(conn, token, now_ms=int(DAY * 1000 * 1.6), ttl=DAY) is None


def test_verify_does_not_write_on_every_request(tmp_path):
    """verify runs on every HLS segment, so it must not write each time."""
    conn = fresh(tmp_path)
    token = issue(conn, now_ms=0)
    before = list(conn.execute("SELECT last_seen_ms FROM sessions"))[0]["last_seen_ms"]
    for offset in range(0, sessions.TOUCH_INTERVAL_SECONDS * 1000, 1000):
        check(conn, token, now_ms=offset)
    after = list(conn.execute("SELECT last_seen_ms FROM sessions"))[0]["last_seen_ms"]
    assert after == before

    check(conn, token, now_ms=sessions.TOUCH_INTERVAL_SECONDS * 1000 + 1)
    moved = list(conn.execute("SELECT last_seen_ms FROM sessions"))[0]["last_seen_ms"]
    assert moved > before


# ------------------------------------------------------------------ pruning


def test_prune_removes_expired_rows(tmp_path):
    conn = fresh(tmp_path)
    issue(conn, now_ms=0, ttl=DAY)
    keeper = issue(conn, now_ms=0, ttl=30 * DAY)
    assert sessions.prune(conn, now_ms=DAY * 1000 + 1) == 1
    assert check(conn, keeper, now_ms=DAY * 1000 + 1) is not None


def test_the_table_is_bounded(tmp_path):
    conn = fresh(tmp_path)
    for index in range(sessions.MAX_SESSIONS + 25):
        issue(conn, now_ms=index * 1000)
    assert sessions.count(conn) <= sessions.MAX_SESSIONS


def test_the_oldest_sessions_go_first_when_bounded(tmp_path):
    conn = fresh(tmp_path)
    first = issue(conn, now_ms=0)
    for index in range(sessions.MAX_SESSIONS + 5):
        issue(conn, now_ms=(index + 1) * 1000)
    newest = issue(conn, now_ms=10_000_000)
    assert check(conn, first, now_ms=10_000_000) is None
    assert check(conn, newest, now_ms=10_000_000) is not None
