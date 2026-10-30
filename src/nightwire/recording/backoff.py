"""Restart backoff for a supervised process. Pure, standard library only.

restart on exit with backoff 2 s, 4 s, 8 s and
so on, capped at 60 s, and reset the sequence after 5 minutes of healthy
running. The event listener uses the same shape.
"""

from __future__ import annotations

from dataclasses import dataclass

FIRST_DELAY = 2.0
MAX_DELAY = 60.0
HEALTHY_AFTER = 300.0


@dataclass
class Backoff:
    first: float = FIRST_DELAY
    maximum: float = MAX_DELAY
    healthy_after: float = HEALTHY_AFTER
    attempt: int = 0

    def next_delay(self, ran_for: float | None = None) -> float:
        """Delay before the next restart.

        `ran_for` is how long the process stayed up. A process that ran longer
        than `healthy_after` counts as recovered, so the sequence restarts
        from the first delay instead of punishing a camera that was fine for
        an hour and then blipped.
        """
        if ran_for is not None and ran_for >= self.healthy_after:
            self.attempt = 0
        delay = min(self.first * (2**self.attempt), self.maximum)
        self.attempt += 1
        return delay

    def reset(self) -> None:
        self.attempt = 0

    @property
    def attempts(self) -> int:
        return self.attempt
