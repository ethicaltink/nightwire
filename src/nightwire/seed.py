"""Seeding cameras from /config/config.yaml.

Optional, and only additive by default: a camera in the YAML that is not in
the database gets inserted, existing rows are left alone. Setting
NVR_CONFIG_AUTHORITATIVE=true makes the file win instead, so a redeploy can
be fully declarative.

The mapping and ${VAR} expansion are pure functions, testable without PyYAML.
Only `load_file` needs the package, and it fails loudly rather than silently
ignoring a file the operator wrote.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from pathlib import Path

from . import cameras

log = logging.getLogger("nightwire.seed")

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

SETTING_KEYS = (
    "retention_days",
    "max_disk_percent",
    "target_disk_percent",
    "segment_seconds",
    "max_exports_gb",
    "station_reload_hour",
)


class SeedError(RuntimeError):
    """The seed file is unusable."""


def expand_env(value, env: dict[str, str] | None = None):
    """Replace ${VAR} with the environment value.

    A missing variable raises rather than expanding to an empty string: an
    empty camera password would otherwise look like a deliberate choice and
    fail later with a confusing ONVIF 401.
    """
    env = dict(os.environ if env is None else env)
    if not isinstance(value, str):
        return value

    missing: list[str] = []

    def substitute(match: re.Match) -> str:
        name = match.group(1)
        if name not in env:
            missing.append(name)
            return ""
        return env[name]

    expanded = ENV_PATTERN.sub(substitute, value)
    if missing:
        raise SeedError(
            f"config.yaml references {', '.join(sorted(set(missing)))}, which "
            "is not set in the environment. Add it to .env or the compose file."
        )
    return expanded


def camera_specs(document: dict, env: dict[str, str] | None = None) -> list[dict]:
    """Normalise the `cameras:` list, expanding environment references."""
    if not isinstance(document, dict):
        raise SeedError("config.yaml must be a mapping at the top level.")
    raw_cameras = document.get("cameras") or []
    if not isinstance(raw_cameras, list):
        raise SeedError("config.yaml `cameras:` must be a list.")

    specs: list[dict] = []
    for index, raw in enumerate(raw_cameras):
        if not isinstance(raw, dict):
            raise SeedError(f"config.yaml camera #{index + 1} must be a mapping.")
        spec = {key: expand_env(value, env) for key, value in raw.items()}
        name = str(spec.get("name") or "").strip()
        host = str(spec.get("host") or "").strip()
        if not host:
            raise SeedError(f"config.yaml camera #{index + 1} has no `host`.")
        camera_id = str(spec.get("id") or "").strip()
        if not camera_id:
            from .onvif import parse

            camera_id = parse.slug_from_name(name or host)
        specs.append(
            {
                "id": camera_id,
                "name": name or camera_id,
                "host": host,
                "onvif_port": int(spec.get("onvif_port") or 80),
                "username": spec.get("username") or None,
                "password": spec.get("password") or None,
                "profile_main": spec.get("profile_main") or None,
                "profile_sub": spec.get("profile_sub") or None,
                "main_uri": spec.get("main_uri") or None,
                "sub_uri": spec.get("sub_uri") or None,
                "retention_days": int(
                    spec.get("retention_days") or document.get("retention_days") or 45
                ),
                "record": bool(spec.get("record", True)),
            }
        )
    return specs


def settings_from(document: dict) -> dict[str, str]:
    """The non-camera settings in the file, as strings for the settings table."""
    return {
        key: str(document[key])
        for key in SETTING_KEYS
        if isinstance(document, dict) and document.get(key) is not None
    }


def load_file(path: Path) -> dict | None:
    """Parse /config/config.yaml, or return None if there is no file."""
    if not path.is_file():
        return None
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise SeedError(
            f"{path} exists but PyYAML is not installed, so it cannot be read. "
            "Install it with `pip install -e .` or rebuild the image. Remove "
            "the file if you no longer want it seeded."
        ) from exc
    try:
        document = yaml.safe_load(path.read_text())
    except Exception as exc:  # noqa: BLE001 - yaml raises several error types
        raise SeedError(f"{path} is not valid YAML: {exc}") from exc
    if document is None:
        return None
    return document


def apply(
    conn: sqlite3.Connection,
    secret: str,
    document: dict,
    *,
    env: dict[str, str] | None = None,
    authoritative: bool = False,
) -> dict:
    """Insert missing cameras and settings. Returns what changed.

    A camera whose stream URI cannot be determined without talking to the
    camera is still inserted when the YAML gives `main_uri` directly. When it
    only names ONVIF profiles, resolution needs the network, so the row is
    skipped and reported: seeding does not probe during boot, because a
    camera that is down must not stop the app from starting.
    """
    env = dict(os.environ if env is None else env)
    specs = camera_specs(document, env)
    existing = cameras.taken_ids(conn)

    created: list[str] = []
    updated: list[str] = []
    skipped: list[dict] = []

    for spec in specs:
        if spec["id"] in existing and not authoritative:
            continue
        if not spec["main_uri"]:
            skipped.append(
                {
                    "id": spec["id"],
                    "reason": (
                        "names ONVIF profiles but no main_uri, so it needs a probe. "
                        "Add the camera in Nodes, or put main_uri in config.yaml."
                    ),
                }
            )
            continue
        if spec["id"] in existing:
            cameras.update(
                conn,
                secret,
                spec["id"],
                {
                    "name": spec["name"],
                    "host": spec["host"],
                    "onvif_port": spec["onvif_port"],
                    "username": spec["username"],
                    "main_uri": spec["main_uri"],
                    "sub_uri": spec["sub_uri"],
                    "retention_days": spec["retention_days"],
                    "record": spec["record"],
                },
                password=spec["password"],
            )
            updated.append(spec["id"])
            continue
        cameras.create(
            conn,
            secret,
            camera_id=spec["id"],
            name=spec["name"],
            host=spec["host"],
            onvif_port=spec["onvif_port"],
            username=spec["username"],
            password=spec["password"],
            main_uri=spec["main_uri"],
            sub_uri=spec["sub_uri"],
            retention_days=spec["retention_days"],
            record=spec["record"],
            main_profile=spec["profile_main"],
            sub_profile=spec["profile_sub"],
        )
        created.append(spec["id"])

    settings = settings_from(document)
    for key, value in settings.items():
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    conn.commit()

    result = {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "settings": sorted(settings),
    }
    if created or updated or skipped:
        log.info("config.yaml seeding: %s", result)
    return result


def seed_from_config_dir(
    conn: sqlite3.Connection,
    secret: str,
    config_dir: Path,
    *,
    env: dict[str, str] | None = None,
) -> dict | None:
    """Called at startup. Never fatal: a bad seed file is logged, not raised."""
    env = dict(os.environ if env is None else env)
    path = config_dir / "config.yaml"
    try:
        document = load_file(path)
        if document is None:
            return None
        authoritative = (env.get("NVR_CONFIG_AUTHORITATIVE") or "").lower() in {
            "1",
            "true",
            "yes",
        }
        return apply(conn, secret, document, env=env, authoritative=authoritative)
    except (SeedError, cameras.CameraError) as exc:
        log.error("config.yaml not applied: %s", exc)
        return {"error": str(exc)}
