from nightwire.recording.backoff import Backoff


def test_sequence_doubles_from_two_seconds():
    b = Backoff()
    assert [b.next_delay() for _ in range(5)] == [2.0, 4.0, 8.0, 16.0, 32.0]


def test_sequence_is_capped_at_sixty():
    b = Backoff()
    delays = [b.next_delay() for _ in range(12)]
    assert max(delays) == 60.0
    assert delays[-1] == 60.0


def test_a_healthy_run_resets_the_sequence():
    b = Backoff()
    for _ in range(6):
        b.next_delay()
    assert b.next_delay(ran_for=301.0) == 2.0


def test_a_short_run_does_not_reset():
    b = Backoff()
    b.next_delay()
    assert b.next_delay(ran_for=10.0) == 4.0


def test_explicit_reset():
    b = Backoff()
    for _ in range(4):
        b.next_delay()
    b.reset()
    assert b.next_delay() == 2.0


def test_attempts_are_counted():
    b = Backoff()
    b.next_delay()
    b.next_delay()
    assert b.attempts == 2
