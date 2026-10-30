"""Per-client login throttling. Standard library only.

One shared password with no throttle is trivially brute-forced by anything
already on the LAN, so: a fixed delay on every attempt, exponential backoff
per client after `backoff_after` failures, and a hard lockout after
`lockout_after`.

State is per client address and is only ever added on a failed attempt, so it
is pruned on every check: without that, an attacker rotating source addresses
grows the dict without limit and the process is the thing that falls over.
See `_evict_expired`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after: int = 0
    reason: str = ""


@dataclass
class _State:
    failures: int = 0
    blocked_until: float = 0.0


@dataclass
class LoginLimiter:
    min_delay_seconds: float = 1.0
    backoff_after: int = 5
    lockout_after: int = 10
    lockout_seconds: int = 900
    max_backoff_seconds: int = 60
    clock: "callable" = time.monotonic
    forget_after_seconds: int = 3600
    max_clients: int = 4096
    _clients: dict[str, _State] = field(default_factory=dict)

    def _evict_expired(self) -> None:
        """Drop clients whose block has expired and who have gone quiet.

        A client is kept while it is blocked, and for `forget_after_seconds`
        after that so a slow brute-force cannot reset its own failure count by
        waiting out the backoff. Beyond `max_clients` the oldest entries go
        regardless, because a bounded dict that occasionally forgets a
        attacker is better than an unbounded one that exhausts memory.
        """
        now = self.clock()
        expired = [
            key
            for key, state in self._clients.items()
            if state.blocked_until and now - state.blocked_until > self.forget_after_seconds
        ]
        for key in expired:
            del self._clients[key]

        if len(self._clients) > self.max_clients:
            ordered = sorted(self._clients.items(), key=lambda item: item[1].blocked_until)
            for key, _ in ordered[: len(self._clients) - self.max_clients]:
                del self._clients[key]

    def check(self, client: str) -> Decision:
        self._evict_expired()
        state = self._clients.get(client)
        if state is None:
            return Decision(allowed=True)
        remaining = state.blocked_until - self.clock()
        if remaining > 0:
            reason = "locked out" if state.failures >= self.lockout_after else "too many attempts"
            return Decision(allowed=False, retry_after=int(remaining) + 1, reason=reason)
        return Decision(allowed=True)

    def record_failure(self, client: str) -> Decision:
        self._evict_expired()
        state = self._clients.setdefault(client, _State())
        state.failures += 1
        now = self.clock()
        if state.failures >= self.lockout_after:
            state.blocked_until = now + self.lockout_seconds
        elif state.failures >= self.backoff_after:
            over = state.failures - self.backoff_after
            delay = min(2**over, self.max_backoff_seconds)
            state.blocked_until = now + delay
        return self.check(client)

    def record_success(self, client: str) -> None:
        self._clients.pop(client, None)

    def failures(self, client: str) -> int:
        state = self._clients.get(client)
        return 0 if state is None else state.failures
