"""Logging setup, with optional structured JSON output.

Structured logs are available. `NVR_LOG_FORMAT=json` switches
every line to one JSON object, which is what a log shipper or `jq` wants;
the default stays human-readable, because the first thing anyone does with a
new container is read its log by eye.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from . import rtsp

HUMAN_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# Attributes LogRecord always carries. Anything else a caller attached is
# extra context and is worth keeping in the JSON line.
_STANDARD = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
        "pathname", "process", "processName", "relativeCreated", "stack_info",
        "taskName", "thread", "threadName",
    }
)


class RedactFilter(logging.Filter):
    """Scrub URL credentials from every log line.

    Belt and braces. The places that handle an authenticated RTSP URL redact
    at the source, but a credentialed URL reaches the log by routes nobody
    enumerated in advance: an httpx or ffmpeg exception string, a traceback
    repr, a third-party library logging its own request. Doing it once on the
    root handler means a missed call site costs nothing.

    Applied to the message and its args rather than the formatted line so a
    JSON record's `message` is redacted too.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and "://" in record.msg:
            record.msg = rtsp.redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: (rtsp.redact(value) if isinstance(value, str) else value)
                    for key, value in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    rtsp.redact(value) if isinstance(value, str) else value
                    for value in record.args
                )
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in _STANDARD or key.startswith("_"):
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                value = repr(value)
            payload[key] = value
        return json.dumps(payload, default=str)


def configure(env: dict | None = None) -> str:
    """Set up root logging. Returns the format chosen, for logging itself."""
    env = dict(os.environ if env is None else env)
    level = (env.get("NVR_LOG_LEVEL") or "INFO").upper()
    wanted = (env.get("NVR_LOG_FORMAT") or "human").strip().lower()

    handler = logging.StreamHandler()
    handler.addFilter(RedactFilter())
    if wanted == "json":
        handler.setFormatter(JsonFormatter())
    else:
        wanted = "human"
        handler.setFormatter(logging.Formatter(HUMAN_FORMAT))

    root = logging.getLogger()
    # Replace rather than add, so repeated configuration does not double
    # every line, which it would under uvicorn's own reload.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))
    return wanted
