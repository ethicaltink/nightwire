"""A fake ONVIF PullPoint, so event handling is testable without a camera.

open and close, merging inside 10
seconds, the 10-minute cap, and a reconnect closing anything left open, all
covered with no hardware.

The notifications built here mirror the shape `onvif/client.py` flattens a
real PullMessages response into: a `Topic` holding the path, and a `Message`
with `UtcTime`, `Source` simple items and `Data` simple items.
"""

from __future__ import annotations

from datetime import datetime, timezone

# Deliberately *not* a real Axis topic constant used by production code - the
# app discovers topics from the camera. These are fixtures, shaped like what
# Axis firmware sends, to prove the discovery path handles them.
VMD_TOPIC = "tns1:RuleEngine/tnsaxis:VMD3/vmd3_video_1"
FENCE_TOPIC = "tns1:RuleEngine/tnsaxis:FenceGuard/Crossed"
TAMPER_TOPIC = "tns1:VideoSource/tnsaxis:Tampering"


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def notification(topic: str, at_ms: int, *, source=None, data=None) -> dict:
    """One notification in the shape the parser expects."""
    message: dict = {"UtcTime": iso(at_ms)}
    if source:
        message["Source"] = {
            "SimpleItem": [{"Name": name, "Value": str(value)} for name, value in source.items()]
        }
    if data:
        message["Data"] = {
            "SimpleItem": [{"Name": name, "Value": str(value)} for name, value in data.items()]
        }
    return {"Topic": {"_value_1": topic}, "Message": {"Message": message}}


def motion(at_ms: int, value: bool, *, profile: str = "1", topic: str = VMD_TOPIC) -> dict:
    """A stateful motion notification: state true opens, false closes."""
    return notification(
        topic,
        at_ms,
        source={"VideoSourceConfigurationToken": "0", "ProfileID": profile},
        data={"active": "true" if value else "false"},
    )


def crossing(at_ms: int, *, line: str = "1") -> dict:
    """A one-shot notification: a line was crossed. No state to read."""
    return notification(FENCE_TOPIC, at_ms, source={"LineID": line}, data={"Count": "1"})


def topic_set() -> dict:
    """A GetEventProperties TopicSet, as the flattener receives it.

    Two stateful topics and one one-shot, nested the way real firmware nests
    them, so `topics.flatten` has to walk containers that are not topics.
    """
    return {
        "tns1:RuleEngine": {
            "tnsaxis:VMD3": {
                "vmd3_video_1": {
                    "MessageDescription": {
                        "IsProperty": "true",
                        "Source": {
                            "SimpleItemDescription": [
                                {"Name": "VideoSourceConfigurationToken"},
                                {"Name": "ProfileID"},
                            ]
                        },
                        "Data": {"SimpleItemDescription": [{"Name": "active"}]},
                    }
                }
            },
            "tnsaxis:FenceGuard": {
                "Crossed": {
                    "MessageDescription": {
                        "IsProperty": "false",
                        "Source": {"SimpleItemDescription": [{"Name": "LineID"}]},
                        "Data": {"SimpleItemDescription": [{"Name": "Count"}]},
                    }
                }
            },
        },
        "tns1:VideoSource": {
            "tnsaxis:Tampering": {
                "MessageDescription": {
                    "IsProperty": "true",
                    "Data": {"SimpleItemDescription": [{"Name": "tampering"}]},
                }
            }
        },
    }


class FakePullPoint:
    """Hands out scripted batches of notifications, then raises.

    Raising at the end is what a dropped subscription looks like, so a test
    can check that the listener closes open events on reconnect.
    """

    def __init__(self, batches: list[list[dict]], *, then=None) -> None:
        self.batches = list(batches)
        self.then = then or ConnectionError("subscription dropped")
        self.connected = False
        self.closed = False
        self.renewals = 0
        self.topics: list[str] = []

    async def connect(self, topics=None) -> None:
        self.connected = True
        self.topics = list(topics or [])

    async def pull(self, timeout_seconds: int = 10, limit: int = 50) -> list[dict]:
        if self.batches:
            return self.batches.pop(0)
        raise self.then

    async def renew(self) -> None:
        self.renewals += 1

    async def close(self) -> None:
        self.closed = True
