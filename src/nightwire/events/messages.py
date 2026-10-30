"""Parsing one PullMessages notification. Pure, standard library only.

A notification is a small tree: a `Topic` whose text is the dotted topic
path, and a `Message` carrying `UtcTime`, a `Source` of simple items that
identifies which instance fired, and `Data` of simple items whose value is
the state.

The shapes vary between firmware, so every accessor here is defensive. What
never varies is the meaning: the topic says what, the source says which, the
data says whether it is on or off, and `UtcTime` says when **by the camera's
clock** - which is why `clock.py` exists.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import topics as topics_mod
from .clock import parse_utc

# Unambiguous boolean words. A bare "1" is deliberately absent: ONVIF allows
# it, but so does a counter, and `Count: "1"` on a line-crossing event is not
# a state of "on". Numeric values count only when the field name says state.
TRUTHY = ("true", "yes", "on", "active")
FALSY = ("false", "no", "off", "inactive")
NUMERIC_TRUE = ("1",)
NUMERIC_FALSE = ("0",)
STATE_WORDS = ("state", "active", "motion", "logical", "alarm", "tamper", "value", "detect")


@dataclass(frozen=True)
class Notification:
    topic: str
    source_items: dict
    data_items: dict
    camera_ts_ms: int | None
    value: bool | None

    @property
    def is_stateful(self) -> bool:
        """Whether this message carries an on/off state at all."""
        return self.value is not None

    def source_key(self, source_names: tuple[str, ...] | None = None) -> str:
        return topics_mod.source_key(
            self.topic, topics_mod.message_source_filter(self.source_items, source_names)
        )


def _text(value) -> str:
    """The text of a node, whatever shape zeep gave it."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("_value_1", "Value", "value", "#text"):
            if key in value and isinstance(value[key], (str, int, float)):
                return str(value[key]).strip()
        return ""
    return str(value).strip()


def _simple_items(container) -> dict:
    """`{"SimpleItem": [{"Name": "x", "Value": "y"}]}` -> `{"x": "y"}`."""
    if not isinstance(container, dict):
        return {}
    items = container.get("SimpleItem")
    if items is None:
        items = container.get("SimpleItemDescription")
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return {}
    out: dict = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("Name") or item.get("name")
        if name is None:
            continue
        out[str(name)] = _text(item.get("Value", item.get("value")))
    return out


def normalise_topic(raw) -> str:
    """The topic path, with the dialect wrapper and whitespace removed."""
    text = _text(raw)
    if not text and isinstance(raw, dict):
        # Some firmware nests the path one level deeper.
        for value in raw.values():
            text = _text(value)
            if text:
                break
    return text.strip().lstrip("/")


def looks_like_state(field_name: str) -> bool:
    """Whether a field name suggests it carries an on/off state."""
    lowered = (field_name or "").lower()
    return any(word in lowered for word in STATE_WORDS)


def interpret_value(data_items: dict) -> bool | None:
    """Whether a message means on, off, or carries no state.

    Field names are not fixed across firmware, so values are what is
    interpreted - but only unambiguous ones. `true` and `false` are read from
    any field; `1` and `0` only from a field whose name suggests state,
    because `Count: "1"` on a line-crossing event means one crossing, not
    "on". A message with no boolean is a one-shot, which has no state.
    """
    booleans: list[tuple[str, bool]] = []
    for key, value in data_items.items():
        text = str(value).strip().lower()
        if text in TRUTHY:
            booleans.append((key, True))
        elif text in FALSY:
            booleans.append((key, False))
        elif looks_like_state(key):
            if text in NUMERIC_TRUE:
                booleans.append((key, True))
            elif text in NUMERIC_FALSE:
                booleans.append((key, False))

    if not booleans:
        return None
    if len(booleans) == 1:
        return booleans[0][1]
    # More than one: prefer the field whose name most suggests state.
    for key, value in booleans:
        if looks_like_state(key):
            return value
    return any(value for _key, value in booleans)


def _utc_time(message: dict):
    """The message's UtcTime, wherever this firmware put it.

    It is an XML attribute, so zeep may surface it as a plain key or inside
    an attribute dict depending on the WSDL binding.
    """
    for key in ("UtcTime", "utcTime"):
        if message.get(key) is not None:
            return message[key]
    attributes = message.get("_attr_1")
    if isinstance(attributes, dict):
        for key, value in attributes.items():
            if str(key).lower() == "utctime":
                return value
    return None


def parse(notification: dict) -> Notification | None:
    """One notification, or None if it is not usable."""
    if not isinstance(notification, dict):
        return None
    topic = normalise_topic(notification.get("Topic"))
    if not topic:
        return None

    message = notification.get("Message") or {}
    if isinstance(message, dict) and "Message" in message:
        # The doubly-wrapped form: NotificationMessage/Message/Message.
        message = message.get("Message") or {}
    if not isinstance(message, dict):
        message = {}

    source_items = _simple_items(message.get("Source"))
    data_items = _simple_items(message.get("Data"))
    camera_ts = parse_utc(_utc_time(message))

    return Notification(
        topic=topic,
        source_items=source_items,
        data_items=data_items,
        camera_ts_ms=camera_ts,
        value=interpret_value(data_items),
    )


def match(notification: Notification, index: dict[str, dict]) -> dict | None:
    """Find the configured source a notification belongs to.

    Tried most specific first: the topic plus the exact source items, then the
    topic plus each single source item, then the topic alone. That ordering is
    what lets one camera have both a per-profile source and a catch-all.
    """
    exact = topics_mod.source_key(
        notification.topic, topics_mod.message_source_filter(notification.source_items)
    )
    if exact in index:
        return index[exact]

    for name in sorted(notification.source_items):
        key = topics_mod.source_key(
            notification.topic,
            topics_mod.message_source_filter(notification.source_items, (name,)),
        )
        if key in index:
            return index[key]

    plain = topics_mod.source_key(notification.topic, "")
    return index.get(plain)
