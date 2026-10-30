"""Camera password encryption at rest.

Fernet, keyed off NVR_SECRET_KEY, as the spec requires. The key derivation is
standard library so it is testable without `cryptography` installed; only the
encrypt and decrypt calls need the package, and they import it lazily so the
rest of the app starts and `/api/health` answers even if it is missing.

Rotating NVR_SECRET_KEY invalidates every stored camera password. That is the
intended trade: the alternative is a second secret to manage.
"""

from __future__ import annotations

import base64
import hashlib


class CryptoUnavailable(RuntimeError):
    """The `cryptography` package is not installed."""


class DecryptFailed(RuntimeError):
    """Ciphertext did not verify. Usually means NVR_SECRET_KEY changed."""


def derive_key(secret: str) -> bytes:
    """Turn an arbitrary NVR_SECRET_KEY into a valid Fernet key.

    Fernet wants 32 bytes, url-safe base64 encoded. NVR_SECRET_KEY is any
    string of at least 32 characters, so hash it to a fixed width rather than
    making the operator produce a Fernet key by hand.
    """
    digest = hashlib.sha256(secret.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet(secret: str):
    try:
        from cryptography.fernet import Fernet
    except ModuleNotFoundError as exc:  # pragma: no cover - import guard
        raise CryptoUnavailable(
            "The `cryptography` package is required to store camera passwords. "
            "Install it with `pip install -e .` or rebuild the image."
        ) from exc
    return Fernet(derive_key(secret))


def encrypt(secret: str, plaintext: str | None) -> str | None:
    """Encrypt a camera password. None and empty both store as NULL."""
    if not plaintext:
        return None
    return _fernet(secret).encrypt(plaintext.encode()).decode()


def decrypt(secret: str, ciphertext: str | None) -> str | None:
    if not ciphertext:
        return None
    try:
        from cryptography.fernet import InvalidToken
    except ModuleNotFoundError as exc:  # pragma: no cover - import guard
        raise CryptoUnavailable("The `cryptography` package is required.") from exc
    try:
        return _fernet(secret).decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise DecryptFailed(
            "Stored camera password could not be decrypted. NVR_SECRET_KEY has "
            "probably changed; re-enter the password on the camera in Nodes."
        ) from exc


def available() -> bool:
    """Whether encryption can actually be performed in this process."""
    try:
        import cryptography  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True
