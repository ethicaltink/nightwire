from nightwire.ratelimit import LoginLimiter


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def limiter(clock):
    return LoginLimiter(backoff_after=5, lockout_after=10, lockout_seconds=900, clock=clock)


def test_first_attempt_is_allowed():
    assert limiter(FakeClock()).check("10.0.0.1").allowed


def test_four_failures_do_not_block():
    clock = FakeClock()
    limit = limiter(clock)
    for _ in range(4):
        limit.record_failure("10.0.0.1")
    assert limit.check("10.0.0.1").allowed


def test_backoff_starts_at_five_failures():
    clock = FakeClock()
    limit = limiter(clock)
    for _ in range(5):
        limit.record_failure("10.0.0.1")
    decision = limit.check("10.0.0.1")
    assert not decision.allowed
    assert decision.reason == "too many attempts"
    clock.advance(decision.retry_after)
    assert limit.check("10.0.0.1").allowed


def test_backoff_grows_and_is_capped():
    clock = FakeClock()
    limit = limiter(clock)
    waits = []
    for _ in range(9):
        decision = limit.record_failure("10.0.0.1")
        waits.append(decision.retry_after)
        clock.advance(decision.retry_after)
    assert waits[4] < waits[5] < waits[6]
    assert max(waits) <= limit.max_backoff_seconds + 1


def test_lockout_at_ten_failures():
    clock = FakeClock()
    limit = limiter(clock)
    # Wait out each backoff, the way a patient attacker would, but do not
    # advance past the lockout that the tenth failure imposes.
    for attempt in range(10):
        decision = limit.record_failure("10.0.0.1")
        if attempt < 9:
            clock.advance(decision.retry_after)
    decision = limit.check("10.0.0.1")
    assert not decision.allowed
    assert decision.reason == "locked out"
    assert decision.retry_after > 800
    clock.advance(900)
    assert limit.check("10.0.0.1").allowed


def test_success_clears_the_counter():
    clock = FakeClock()
    limit = limiter(clock)
    for _ in range(4):
        limit.record_failure("10.0.0.1")
    limit.record_success("10.0.0.1")
    assert limit.failures("10.0.0.1") == 0


def test_clients_are_tracked_separately():
    clock = FakeClock()
    limit = limiter(clock)
    for _ in range(10):
        limit.record_failure("10.0.0.1")
    assert not limit.check("10.0.0.1").allowed
    assert limit.check("10.0.0.2").allowed


# --------------------------------------------- the dict must not grow forever


def test_an_expired_client_is_forgotten():
    """State is only added on failure, so it has to be pruned."""
    clock = FakeClock()
    limit = LoginLimiter(backoff_after=5, lockout_after=10, lockout_seconds=900,
                         forget_after_seconds=3600, clock=clock)
    for _ in range(10):
        limit.record_failure("10.0.0.1")
    assert len(limit._clients) == 1
    clock.advance(900 + 3601)
    assert limit.check("10.0.0.1").allowed
    assert limit._clients == {}


def test_a_blocked_client_is_kept_while_it_is_blocked():
    clock = FakeClock()
    limit = LoginLimiter(backoff_after=5, lockout_after=10, lockout_seconds=900,
                         forget_after_seconds=3600, clock=clock)
    for _ in range(10):
        limit.record_failure("10.0.0.1")
    clock.advance(800)
    assert not limit.check("10.0.0.1").allowed
    assert limit.failures("10.0.0.1") == 10


def test_a_client_is_kept_through_the_grace_window():
    """Waiting out the backoff must not reset the failure count."""
    clock = FakeClock()
    limit = LoginLimiter(backoff_after=5, lockout_after=10, lockout_seconds=900,
                         forget_after_seconds=3600, clock=clock)
    for _ in range(6):
        limit.record_failure("10.0.0.1")
    clock.advance(600)
    assert limit.check("10.0.0.1").allowed
    assert limit.failures("10.0.0.1") == 6


def test_the_client_table_is_bounded():
    """An attacker rotating source addresses must not exhaust memory."""
    clock = FakeClock()
    limit = LoginLimiter(backoff_after=1, lockout_after=2, lockout_seconds=5,
                         max_clients=50, clock=clock)
    for index in range(500):
        limit.record_failure(f"10.0.{index // 256}.{index % 256}")
        clock.advance(0.01)
    assert len(limit._clients) <= 50
