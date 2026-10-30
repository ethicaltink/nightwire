"""Interpreting what a camera says it can emit. Pure, standard library only.

ONVIF `GetEventProperties` returns a `TopicSet`: a nested tree whose leaves
carry a `MessageDescription`. This module flattens that into a list the Nodes
page can show, and infers whether each topic is stateful.

**No vendor topic string is hard-coded anywhere here**, which the spec insists
on because topic names vary by vendor and firmware. The two facts that matter are
read from the camera's own description:

- `MessageDescription/@IsProperty="true"` means the topic carries state, so
  it produces span events that open and close. Anything else is a one-shot,
  which produces a point event.
- `MessageDescription/Source/SimpleItemDescription/@Name` names the fields
  that distinguish instances sharing a topic, which is how two motion
  profiles on one camera become two separate timeline lanes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SPAN = "span"
POINT = "point"

# Keys that appear in a flattened TopicSet but are not topic nodes.
_META_KEYS = ("MessageDescription", "_value_1", "_attr_1", "_raw_", "Any")
MAX_DEPTH = 12


@dataclass(frozen=True)
class Topic:
    """One thing a camera says it can report."""

    path: str
    is_property: bool
    source_names: tuple[str, ...] = ()
    data_names: tuple[str, ...] = ()

    @property
    def kind(self) -> str:
        """`span` for stateful topics, `point` for one-shots."""
        return SPAN if self.is_property else POINT

    @property
    def label(self) -> str:
        """A readable default the operator can overwrite."""
        return default_label(self.path)

    @property
    def describes_as(self) -> str:
        """What the Nodes page shows in the "Shows as" column."""
        return "Block (on/off)" if self.kind == SPAN else "Diamond (one-off)"

    def as_dict(self) -> dict:
        return {
            "topic": self.path,
            "kind": self.kind,
            "is_property": self.is_property,
            "label": self.label,
            "source_names": list(self.source_names),
            "data_names": list(self.data_names),
            "shows_as": self.describes_as,
        }


def strip_prefix(segment: str) -> str:
    """`tnsaxis:VideoMotionDetection` -> `VideoMotionDetection`.

    Generic XML namespace-prefix removal, not a vendor list.
    """
    return segment.split(":", 1)[1] if ":" in segment else segment


def split_words(segment: str) -> str:
    """`VideoMotionDetection` -> `Video Motion Detection`."""
    out: list[str] = []
    for index, char in enumerate(segment):
        previous = segment[index - 1] if index else ""
        following = segment[index + 1] if index + 1 < len(segment) else ""
        if char.isupper() and index and (previous.islower() or (following and following.islower())):
            out.append(" ")
        out.append(char)
    return "".join(out).replace("_", " ").strip()


def default_label(path: str) -> str:
    """A label from the topic path: the last two meaningful segments.

    Derived rather than looked up, so a topic this code has never seen still
    gets something readable instead of a raw namespaced string.
    """
    segments = [strip_prefix(part) for part in (path or "").split("/") if part]
    if not segments:
        return "Event"
    tail = segments[-2:] if len(segments) > 1 else segments
    words = [split_words(part) for part in tail if part]
    # Drop a trailing repeat, e.g. ".../Motion/Motion".
    if len(words) == 2 and words[0].lower() == words[1].lower():
        words = words[:1]
    return " ".join(words)


def _names(description: dict, key: str) -> tuple[str, ...]:
    """Field names under Source or Data in a MessageDescription."""
    container = description.get(key) or {}
    if not isinstance(container, dict):
        return ()
    items = container.get("SimpleItemDescription") or []
    if isinstance(items, dict):
        items = [items]
    found = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("Name") or item.get("name")
        if name:
            found.append(str(name))
    return tuple(found)


def _is_property(description: dict) -> bool:
    """Read IsProperty, tolerating the shapes zeep produces for attributes."""
    for key in ("IsProperty", "isProperty", "_attr_IsProperty"):
        if key in description:
            value = description[key]
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("true", "1", "yes")
    attributes = description.get("_attr_1") or description.get("attributes")
    if isinstance(attributes, dict):
        for key, value in attributes.items():
            if str(key).lower().endswith("isproperty"):
                return str(value).strip().lower() in ("true", "1", "yes")
    return False


def flatten(topic_set, prefix: str = "", depth: int = 0) -> list[Topic]:
    """Walk a TopicSet tree and return every topic leaf it contains.

    A node is a topic when it carries a `MessageDescription`. Nodes without
    one are namespace containers and are walked through.
    """
    if depth > MAX_DEPTH or not isinstance(topic_set, dict):
        return []

    found: list[Topic] = []
    for key, value in topic_set.items():
        if not isinstance(key, str) or key.startswith("_") or key in _META_KEYS:
            continue
        if not isinstance(value, dict):
            continue

        path = f"{prefix}/{key}" if prefix else key
        description = value.get("MessageDescription")
        if isinstance(description, dict):
            found.append(
                Topic(
                    path=path,
                    is_property=_is_property(description),
                    source_names=_names(description, "Source"),
                    data_names=_names(description, "Data"),
                )
            )
        found.extend(flatten(value, path, depth + 1))

    return sorted(found, key=lambda topic: topic.path)


def source_key(topic: str, source_filter: str | None) -> str:
    """The identity of one event source, for matching incoming messages."""
    return f"{topic}#{source_filter or ''}"


def message_source_filter(source_items: dict | None, names: tuple[str, ...] | None = None) -> str:
    """Build a source filter from a message's own Source items.

    A message carries the values of the fields the topic declared, for
    example `{"VideoSourceConfigurationToken": "0", "ProfileID": "2"}`. Those
    values joined in a stable order are what tells two motion profiles on one
    camera apart.
    """
    if not source_items:
        return ""
    keys = [key for key in sorted(source_items) if names is None or key in names]
    return ",".join(f"{key}={source_items[key]}" for key in keys)
