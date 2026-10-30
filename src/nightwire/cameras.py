"""Camera records: create, read, update, delete.

Standard library only at import time, so this is testable without the ONVIF
stack. Passwords go through `crypto`, which only needs `cryptography` when
actually encrypting.

A camera's `id` is a slug that ends up in filesystem paths under /recordings
and in URLs, so it is validated here rather than trusted from the client.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import crypto, rtsp
from .onvif import parse

log = logging.getLogger("nightwire.cameras")

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")

# Columns the client may set. `id` is set once at creation; the rest of the
# table is maintained by the app.
EDITABLE = frozenset(
    {
        "name",
        "host",
        "onvif_port",
        "username",
        "main_uri",
        "sub_uri",
        "record",
        "retention_days",
        "sort_order",
        "main_profile",
        "sub_profile",
    }
)

# Never leaves the process.
SECRET_COLUMNS = frozenset({"password_enc"})


class CameraError(ValueError):
    """Bad input for a camera record."""


class NotFound(LookupError):
    """No camera with that id."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_id(camera_id: str) -> str:
    if not camera_id or not ID_PATTERN.match(camera_id):
        raise CameraError(
            "Camera id must be 2 to 32 characters of lowercase letters, digits "
            f"and hyphens, starting with a letter or digit. Got {camera_id!r}."
        )
    return camera_id


def taken_ids(conn: sqlite3.Connection) -> set[str]:
    return {row["id"] for row in conn.execute("SELECT id FROM cameras")}


def suggest_id(conn: sqlite3.Connection, name: str) -> str:
    return parse.slug_from_name(name, taken=taken_ids(conn))


def to_public(row: sqlite3.Row) -> dict:
    """A camera as the API returns it: no secrets, metadata decoded."""
    data = {key: row[key] for key in row.keys() if key not in SECRET_COLUMNS}
    data["has_password"] = bool(row["password_enc"])
    for field in ("main_meta", "sub_meta"):
        raw = data.pop(field, None)
        try:
            data[field.replace("_meta", "_stream")] = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            data[field.replace("_meta", "_stream")] = None
    data["record"] = bool(row["record"])
    return data


def list_cameras(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM cameras ORDER BY sort_order, id")
    return [to_public(row) for row in rows]


def get_row(conn: sqlite3.Connection, camera_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM cameras WHERE id = ?", (camera_id,)).fetchone()
    if row is None:
        raise NotFound(camera_id)
    return row


def get(conn: sqlite3.Connection, camera_id: str) -> dict:
    return to_public(get_row(conn, camera_id))


def credentials(conn: sqlite3.Connection, camera_id: str, secret: str) -> tuple[str | None, str | None]:
    """The username and decrypted password for talking to this camera."""
    row = get_row(conn, camera_id)
    return row["username"], crypto.decrypt(secret, row["password_enc"])


def authenticated_uri(row, secret: str, which: str = "main") -> str:
    """A camera's stream URI with credentials injected for a child process.

    The result is handed straight to ffmpeg's or go2rtc's argv and **must not
    be stored or logged**: use `rtsp.redact` on anything derived from it. The
    credentials are unavoidably visible in the spawned process's cmdline,
    which is how ffmpeg takes them.
    """
    uri = row["main_uri"] if which == "main" else (row["sub_uri"] or "")
    if not uri:
        return ""
    if not row["username"]:
        return uri
    try:
        password = crypto.decrypt(secret, row["password_enc"])
    except (crypto.CryptoUnavailable, crypto.DecryptFailed) as exc:
        log.warning("camera %s: could not decrypt the password (%s)", row["id"], exc)
        return uri
    return rtsp.with_credentials(uri, row["username"], password)


def create(
    conn: sqlite3.Connection,
    secret: str,
    *,
    camera_id: str,
    name: str,
    host: str,
    main_uri: str,
    onvif_port: int = 80,
    username: str | None = None,
    password: str | None = None,
    sub_uri: str | None = None,
    model: str | None = None,
    firmware: str | None = None,
    serial: str | None = None,
    retention_days: int = 45,
    record: bool = True,
    main_profile: str | None = None,
    sub_profile: str | None = None,
    main_meta: dict | None = None,
    sub_meta: dict | None = None,
    event_source_count: int | None = None,
) -> dict:
    validate_id(camera_id)
    if not name.strip():
        raise CameraError("Camera name is required.")
    if not host.strip():
        raise CameraError("Camera address is required.")
    if not main_uri.strip():
        raise CameraError(
            "A main stream URI is required. Probe the camera first, or enter an "
            "RTSP URL directly."
        )
    if int(retention_days) < 1:
        raise CameraError("Retention must be at least 1 day.")

    if camera_id in taken_ids(conn):
        raise CameraError(f"A camera with id {camera_id!r} already exists.")

    # Credentials never go in the URI column. ffmpeg and go2rtc need them in
    # the URL, but they are injected at spawn time from `password_enc`; a URI
    # pasted with credentials in it would otherwise store a plaintext camera
    # password in the database and in every backup of it.
    if rtsp.has_credentials(main_uri) or rtsp.has_credentials(sub_uri or ""):
        embedded_user, embedded_pass = rtsp.split_userinfo(main_uri)
        if not username and embedded_user:
            username = embedded_user
        if not password and embedded_pass:
            password = embedded_pass
        log.info("camera %s: moved credentials out of the stream URI", camera_id)
    main_uri = rtsp.strip_credentials(main_uri)
    sub_uri = rtsp.strip_credentials(sub_uri) if sub_uri else sub_uri

    conn.execute(
        """
        INSERT INTO cameras (
            id, name, host, onvif_port, username, password_enc,
            main_uri, sub_uri, model, firmware, serial,
            record, retention_days, sort_order, created_at,
            main_profile, sub_profile, main_meta, sub_meta,
            event_source_count, last_probe_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            camera_id,
            name.strip(),
            host.strip(),
            int(onvif_port),
            (username or "").strip() or None,
            crypto.encrypt(secret, password),
            main_uri.strip(),
            (sub_uri or "").strip() or None,
            model,
            firmware,
            serial,
            1 if record else 0,
            int(retention_days),
            _next_sort_order(conn),
            _now(),
            main_profile,
            sub_profile,
            json.dumps(main_meta) if main_meta else None,
            json.dumps(sub_meta) if sub_meta else None,
            event_source_count,
            _now(),
        ),
    )
    conn.commit()
    return get(conn, camera_id)


def _next_sort_order(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS next FROM cameras").fetchone()
    return int(row["next"])


def update(
    conn: sqlite3.Connection,
    secret: str,
    camera_id: str,
    changes: dict,
    *,
    password: str | None = None,
    clear_password: bool = False,
) -> dict:
    """Patch a camera. Only EDITABLE columns may be set from a request."""
    get_row(conn, camera_id)

    unknown = set(changes) - EDITABLE
    if unknown:
        raise CameraError(f"Cannot change: {', '.join(sorted(unknown))}.")

    assignments = []
    values: list = []
    for column, value in changes.items():
        if column == "record":
            value = 1 if value else 0
        if column == "retention_days" and int(value) < 1:
            raise CameraError("Retention must be at least 1 day.")
        if column in {"name", "host"} and not str(value).strip():
            raise CameraError(f"{column} cannot be empty.")
        if column in {"main_uri", "sub_uri"} and value:
            if rtsp.has_credentials(str(value)):
                log.info("camera %s: moved credentials out of %s", camera_id, column)
            value = rtsp.strip_credentials(str(value))
        assignments.append(f"{column} = ?")
        values.append(value)

    if clear_password:
        assignments.append("password_enc = ?")
        values.append(None)
    elif password:
        assignments.append("password_enc = ?")
        values.append(crypto.encrypt(secret, password))

    if not assignments:
        return get(conn, camera_id)

    values.append(camera_id)
    conn.execute(f"UPDATE cameras SET {', '.join(assignments)} WHERE id = ?", values)
    conn.commit()
    return get(conn, camera_id)


def record_probe(
    conn: sqlite3.Connection,
    camera_id: str,
    *,
    device: dict | None = None,
    main_meta: dict | None = None,
    sub_meta: dict | None = None,
    event_source_count: int | None = None,
    error: str | None = None,
) -> None:
    """Store the outcome of a probe or a connection test."""
    get_row(conn, camera_id)
    assignments = ["last_probe_at = ?", "last_error = ?"]
    values: list = [_now(), error]
    if device:
        for column, key in (("model", "model"), ("firmware", "firmware"), ("serial", "serial")):
            if device.get(key):
                assignments.append(f"{column} = ?")
                values.append(device[key])
    if main_meta is not None:
        assignments.append("main_meta = ?")
        values.append(json.dumps(main_meta))
    if sub_meta is not None:
        assignments.append("sub_meta = ?")
        values.append(json.dumps(sub_meta))
    if event_source_count is not None:
        assignments.append("event_source_count = ?")
        values.append(event_source_count)
    values.append(camera_id)
    conn.execute(f"UPDATE cameras SET {', '.join(assignments)} WHERE id = ?", values)
    conn.commit()


def delete(
    conn: sqlite3.Connection,
    camera_id: str,
    *,
    recordings_dir: Path | None = None,
    delete_recordings: bool = False,
) -> dict:
    """Remove a camera, and optionally its footage.

    Footage deletion is opt-in and never the default: the spec has removal ask
    whether to delete recordings, and a mis-click must not destroy 45 days of
    video. Rows in `segments` and `events` are removed either way, because
    they reference a camera that no longer exists.
    """
    get_row(conn, camera_id)
    removed_paths = 0

    conn.execute("DELETE FROM events WHERE camera_id = ?", (camera_id,))
    conn.execute("DELETE FROM event_sources WHERE camera_id = ?", (camera_id,))
    conn.execute("DELETE FROM segments WHERE camera_id = ?", (camera_id,))
    conn.execute("DELETE FROM cameras WHERE id = ?", (camera_id,))
    conn.commit()

    if delete_recordings and recordings_dir is not None:
        for folder in (
            recordings_dir / camera_id,
            recordings_dir / "_thumbs" / camera_id,
            recordings_dir / "_events" / camera_id,
        ):
            # Guard against a traversal via a crafted id, even though
            # validate_id already rejects separators.
            resolved = folder.resolve()
            if recordings_dir.resolve() not in resolved.parents:
                continue
            if resolved.is_dir():
                shutil.rmtree(resolved, ignore_errors=True)
                removed_paths += 1

    return {"deleted": camera_id, "recordings_deleted": removed_paths}
