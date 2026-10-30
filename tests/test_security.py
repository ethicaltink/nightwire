"""Password checking and session token primitives. Pure, stdlib only."""

from nightwire import security

SECRET = "s" * 40


def test_password_check():
    assert security.check_password("hunter2hunter2", "hunter2hunter2")
    assert not security.check_password("hunter2hunter2", "wrong")
    assert not security.check_password("hunter2hunter2", "")
    assert not security.check_password("hunter2hunter2", None)


def test_tokens_are_unique_and_wide():
    tokens = {security.new_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(len(token) >= 40 for token in tokens)


def test_token_id_is_stable_and_not_the_token():
    token = security.new_token()
    assert security.token_id(token) == security.token_id(token)
    assert token not in security.token_id(token)
    assert len(security.token_id(token)) == 64


def test_token_id_differs_per_token():
    assert security.token_id("a") != security.token_id("b")


def test_token_id_on_nothing():
    """A missing cookie must not raise on the way to being rejected."""
    assert len(security.token_id("")) == 64
    assert len(security.token_id(None)) == 64


def test_password_fingerprint_changes_with_the_password():
    first = security.password_fingerprint(SECRET, "hunter2hunter2")
    second = security.password_fingerprint(SECRET, "hunter3hunter3")
    assert first != second
    assert first == security.password_fingerprint(SECRET, "hunter2hunter2")


def test_password_fingerprint_changes_with_the_secret_key():
    """Rotating NVR_SECRET_KEY invalidates sessions, as it does camera passwords."""
    assert security.password_fingerprint(SECRET, "p") != security.password_fingerprint(
        "other" * 10, "p"
    )


def test_password_fingerprint_does_not_contain_the_password():
    assert "hunter2" not in security.password_fingerprint(SECRET, "hunter2hunter2")


def test_refresh_window():
    day_ms = 86_400_000
    assert not security.needs_refresh(1000, refresh_after_seconds=86400, now_ms=1000)
    assert security.needs_refresh(1000, refresh_after_seconds=86400, now_ms=1000 + day_ms)
