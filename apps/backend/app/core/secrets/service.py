"""Fernet-based symmetric encryption for at-rest application secrets.

The master key is the existing `yaaos_totp_master_key` env var (URL-safe
base64 32-byte key). In non-prod, callers fall back to `yaaos_encryption_key`
so dev/test stacks only need one key. Production must set the dedicated key.

This module is the single owner of the Fernet construction; callers should
not instantiate `cryptography.fernet.Fernet` themselves.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


class SecretsDecryptError(ValueError):
    """Raised when ciphertext cannot be decrypted with the configured master key."""


def _fernet() -> Fernet:
    s = get_settings()
    key = s.yaaos_totp_master_key or s.yaaos_encryption_key
    return Fernet(key.encode())


def encrypt(plaintext: bytes | str) -> bytes:
    """Encrypt bytes/str with the master key. Returns Fernet ciphertext bytes."""
    data = plaintext.encode() if isinstance(plaintext, str) else plaintext
    return _fernet().encrypt(data)


def decrypt(ciphertext: bytes) -> bytes:
    """Decrypt Fernet ciphertext. Raises `SecretsDecryptError` on bad key/data."""
    try:
        return _fernet().decrypt(ciphertext)
    except InvalidToken as exc:
        raise SecretsDecryptError("invalid ciphertext or wrong key") from exc
