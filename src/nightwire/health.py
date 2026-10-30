"""Health reporting.

Healthy means: the database answers, go2rtc is up, and every *enabled*
camera has a running recorder. With no cameras configured the recorder
check passes vacuously, so a fresh install reports healthy.

Each component registers its own probe here as it starts. Until it does, it
reports `state: "not-started"` and does not hold the overall status down,
so a slow start or a disabled component is not an outage.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

from . import db, recorders

# name -> callable returning (ok, state, detail), filled in at startup.
ProbeResult = tuple[bool, str, str]
_probes: dict[str, Callable[[], ProbeResult]] = {}

# Components that report themselves. Listed so one that never starts is
# still visible in the report rather than silently absent.
_COMPONENTS = ("go2rtc", "recorders", "events")


def register(name: str, probe: Callable[[], ProbeResult]) -> None:
    """Register a live component probe, replacing its not-started placeholder."""
    _probes[name] = probe


def reset() -> None:
    """Drop all registered probes. Tests use this; nothing else should."""
    _probes.clear()


def _database(conn: sqlite3.Connection) -> ProbeResult:
    try:
        conn.execute("SELECT 1").fetchone()
    except sqlite3.Error as exc:
        return False, "error", str(exc)
    return True, "ok", "sqlite reachable"


def _recorders(conn: sqlite3.Connection) -> ProbeResult:
    if "recorders" in _probes:
        return _probes["recorders"]()
    return recorders.health(len(db.enabled_cameras(conn)))


def report(conn: sqlite3.Connection) -> tuple[bool, dict]:
    """Return (healthy, body). Body always names every component."""
    components: dict[str, dict] = {}

    ok, state, detail = _database(conn)
    components["database"] = {"ok": ok, "state": state, "detail": detail}

    ok, state, detail = _recorders(conn)
    components["recorders"] = {"ok": ok, "state": state, "detail": detail}

    for name in _COMPONENTS:
        if name == "recorders":
            continue
        if name in _probes:
            ok, state, detail = _probes[name]()
        else:
            ok, state, detail = True, "not-started", "not running"
        components[name] = {"ok": ok, "state": state, "detail": detail}

    healthy = all(component["ok"] for component in components.values())
    return healthy, {"status": "healthy" if healthy else "unhealthy", "components": components}
