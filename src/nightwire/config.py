"""Environment configuration and startup validation.

The app refuses to start on a missing or example-value secret rather than
booting with a known key, because NVR_SECRET_KEY encrypts the camera
passwords and signs the session cookies.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

MIN_PASSWORD_LEN = 8
MIN_SECRET_LEN = 32

# Values that look configured but are not. Checked case-insensitively.
EXAMPLE_VALUES = frozenset(
    {
        "",
        "change-me",
        "changeme",
        "change_me",
        "replace-me",
        "replaceme",
        "password",
        "secret",
        "your-password-here",
        "todo",
        "xxx",
    }
)


class ConfigError(RuntimeError):
    """Raised when the environment is not fit to start with."""


@dataclass(frozen=True)
class Settings:
    password: str
    secret_key: str
    port: int
    timezone: str
    config_dir: Path
    recordings_dir: Path
    static_dir: Path
    tmp_dir: Path = Path("/tmp/nvr")
    session_days: int = 30
    # Re-issue the cookie once it is older than this, so a station tab left
    # open for weeks stays signed in.
    session_refresh_days: int = 1

    @property
    def db_path(self) -> Path:
        return self.config_dir / "nightwire.db"

    def tzinfo(self):
        """The zone segment filenames are written in.

        ffmpeg's -strftime uses the process clock, so the recorder and the
        indexer must agree on the zone. An unknown TZ falls back to the system
        local zone rather than refusing to start.
        """
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError, ModuleNotFoundError):
            return None


def _require(env: dict[str, str], key: str, minimum: int) -> str:
    value = (env.get(key) or "").strip()
    if value.lower() in EXAMPLE_VALUES:
        raise ConfigError(
            f"{key} is empty or still an example value. Set a real one in .env "
            f"(see .env.example); the app will not start without it."
        )
    if len(value) < minimum:
        raise ConfigError(f"{key} must be at least {minimum} characters, got {len(value)}.")
    return value


def _port(env: dict[str, str]) -> int:
    raw = (env.get("NVR_PORT") or "8080").strip()
    try:
        port = int(raw)
    except ValueError as exc:
        raise ConfigError(f"NVR_PORT must be an integer, got {raw!r}.") from exc
    if not 1 <= port <= 65535:
        raise ConfigError(f"NVR_PORT out of range: {port}.")
    return port


def load(env: dict[str, str] | None = None) -> Settings:
    """Build Settings from the environment, raising ConfigError if unusable."""
    env = dict(os.environ if env is None else env)
    package_static = Path(__file__).resolve().parent / "static"
    return Settings(
        password=_require(env, "NVR_PASSWORD", MIN_PASSWORD_LEN),
        secret_key=_require(env, "NVR_SECRET_KEY", MIN_SECRET_LEN),
        port=_port(env),
        timezone=(env.get("TZ") or "UTC").strip(),
        config_dir=Path(env.get("NVR_CONFIG_DIR") or "/config"),
        recordings_dir=Path(env.get("NVR_RECORDINGS_DIR") or "/recordings"),
        static_dir=Path(env.get("NVR_STATIC_DIR") or package_static),
        tmp_dir=Path(env.get("NVR_TMP_DIR") or "/tmp/nvr"),
    )
