import base64

from nightwire import crypto


def test_derive_key_is_a_valid_fernet_key():
    key = crypto.derive_key("s" * 40)
    assert len(base64.urlsafe_b64decode(key)) == 32


def test_derive_key_is_deterministic():
    assert crypto.derive_key("secret-secret-secret-secret-1234") == crypto.derive_key(
        "secret-secret-secret-secret-1234"
    )


def test_different_secrets_derive_different_keys():
    assert crypto.derive_key("a" * 40) != crypto.derive_key("b" * 40)


def test_empty_password_stores_as_null():
    assert crypto.encrypt("s" * 40, None) is None
    assert crypto.encrypt("s" * 40, "") is None
    assert crypto.decrypt("s" * 40, None) is None


def test_round_trip():
    if not crypto.available():
        return  # cryptography is not installed in this environment
    secret = "s" * 40
    token = crypto.encrypt(secret, "camera-password")
    assert token is not None
    assert "camera-password" not in token
    assert crypto.decrypt(secret, token) == "camera-password"


def test_a_rotated_secret_key_fails_loudly():
    if not crypto.available():
        return
    token = crypto.encrypt("a" * 40, "camera-password")
    try:
        crypto.decrypt("b" * 40, token)
    except crypto.DecryptFailed as exc:
        assert "NVR_SECRET_KEY" in str(exc)
    else:
        raise AssertionError("expected DecryptFailed")
