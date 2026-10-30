"""Recorder state, as the Nodes page and /api/health both report it.

The running supervisor registers itself here. Until it does - during
startup, or when `NVR_DISABLE_RECORDER=1` is set - this is the single place
that says so, which keeps the two callers from drifting apart or implying a
working camera is broken.

States: recording, reconnecting, error, disabled, plus the not-started
placeholder returned before the supervisor is up.
"""

from __future__ import annotations

from typing import Protocol

class Supervisor(Protocol):
    def status(self, camera_id: str) -> dict:  # pragma: no cover - interface
        ...


_supervisor: Supervisor | None = None


def register(supervisor: Supervisor | None) -> None:
    """Called once the ffmpeg supervisor is running."""
    global _supervisor
    _supervisor = supervisor


def reset() -> None:
    """Tests use this; nothing else should."""
    register(None)


def running() -> bool:
    return _supervisor is not None


def status_for(camera_id: str, *, enabled: bool = True) -> dict:
    """What this camera's recorder is doing, in the shape the UI expects."""
    if _supervisor is not None:
        return _supervisor.status(camera_id)
    if not enabled:
        return {
            "state": "disabled",
            "detail": "Recording is switched off for this camera.",
            "last_segment_at": None,
            "stderr_tail": [],
        }
    return {
        "state": "not-started",
        "detail": "The recorder supervisor is not running. Nothing is being recorded yet.",
        "last_segment_at": None,
        "stderr_tail": [],
    }


def health(enabled_camera_count: int) -> tuple[bool, str, str]:
    """The (ok, state, detail) triple /api/health wants for `recorders`.

    With no cameras configured this passes vacuously, which is what makes a
    fresh install healthy. A configured camera with no supervisor yet reports
    not-started and still does not fail health: the supervisor being absent
    is a startup state, not a camera fault.
    """
    if _supervisor is not None and hasattr(_supervisor, "health"):
        return _supervisor.health()  # type: ignore[attr-defined]
    if enabled_camera_count == 0:
        return True, "idle", "no cameras configured"
    return (
        True,
        "not-started",
        f"{enabled_camera_count} camera(s) configured; the recorder supervisor is not running",
    )
