"""Structured logging."""

import json
import logging

from nightwire import logs


def record(**extra):
    base = logging.LogRecord(
        name="nightwire.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="camera %s started",
        args=("front",),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(base, key, value)
    return base


def test_the_json_formatter_emits_one_object():
    line = logs.JsonFormatter().format(record())
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "nightwire.test"
    assert payload["message"] == "camera front started"
    assert payload["ts"].endswith("+00:00")


def test_extra_context_is_kept():
    payload = json.loads(logs.JsonFormatter().format(record(camera="front", segments=12)))
    assert payload["camera"] == "front"
    assert payload["segments"] == 12


def test_unserialisable_context_does_not_break_the_line():
    payload = json.loads(logs.JsonFormatter().format(record(thing=object())))
    assert isinstance(payload["thing"], str)


def test_an_exception_is_included():
    try:
        raise ValueError("no footage")
    except ValueError:
        import sys

        entry = record()
        entry.exc_info = sys.exc_info()
        payload = json.loads(logs.JsonFormatter().format(entry))
    assert "ValueError" in payload["exception"]


def test_configure_defaults_to_human_readable():
    assert logs.configure({}) == "human"
    assert not isinstance(logging.getLogger().handlers[0].formatter, logs.JsonFormatter)


def test_configure_switches_to_json():
    assert logs.configure({"NVR_LOG_FORMAT": "json"}) == "json"
    assert isinstance(logging.getLogger().handlers[0].formatter, logs.JsonFormatter)
    logs.configure({})  # leave the root logger as the suite found it


def test_an_unknown_format_falls_back_rather_than_failing():
    assert logs.configure({"NVR_LOG_FORMAT": "yaml-please"}) == "human"


def test_configuring_twice_does_not_double_every_line():
    logs.configure({})
    logs.configure({})
    assert len(logging.getLogger().handlers) == 1


def test_the_level_is_honoured():
    logs.configure({"NVR_LOG_LEVEL": "WARNING"})
    assert logging.getLogger().level == logging.WARNING
    logs.configure({})
