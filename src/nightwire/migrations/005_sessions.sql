-- Server-side sessions, from finding 4 of the 2026-10-29 security review.
--
-- A session used to be an HMAC-signed issue time with no database row, so
-- that a container restart would not sign the viewing station out. The cost
-- was that nothing could revoke one: logout cleared the cookie in the
-- browser while its signature stayed valid, and changing NVR_PASSWORD left
-- every issued session working.
--
-- A row per session gets revocation back without giving up the property the
-- original decision was made for, because this database lives on the
-- /config volume and survives the restart.
--
-- `id` is the SHA-256 of the cookie token, never the token: a readable copy
-- of this table is then not a set of usable cookies.
-- `password_fp` is an HMAC of the password that was in force when the
-- session was issued, so a changed NVR_SECRET_KEY or NVR_PASSWORD
-- invalidates it without needing to store either.

CREATE TABLE sessions (
    id           TEXT PRIMARY KEY,
    created_ms   INTEGER NOT NULL,
    expires_ms   INTEGER NOT NULL,
    last_seen_ms INTEGER NOT NULL,
    password_fp  TEXT NOT NULL,
    client       TEXT
);

CREATE INDEX idx_sessions_expires ON sessions(expires_ms);
