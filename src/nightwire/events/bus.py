"""An in-process pub/sub for live event notifications.

The SSE endpoint at `/api/events/stream` needs to push an open or close to
every connected browser the moment it happens. One asyncio queue per
subscriber, with a bounded size: a browser that stops reading loses
notifications rather than growing the server's memory, because the glow is a
nicety and the database is the record.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("nightwire.events.bus")

QUEUE_SIZE = 50


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, message: dict) -> None:
        """Hand a message to every subscriber, dropping it for slow ones."""
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                log.debug("live event dropped for a slow subscriber")


# One bus per process. The listener publishes, the SSE endpoint subscribes.
bus = EventBus()
