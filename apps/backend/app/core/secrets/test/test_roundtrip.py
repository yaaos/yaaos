"""Encrypt/decrypt round-trip + wrong-key rejection."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.core.secrets import SecretsDecryptError, decrypt, encrypt


def test_encrypt_decrypt_roundtrip_str() -> None:
    ciphertext = encrypt("hunter2")
    assert decrypt(ciphertext) == b"hunter2"


def test_encrypt_decrypt_roundtrip_bytes() -> None:
    ciphertext = encrypt(b"\x00\x01\x02")
    assert decrypt(ciphertext) == b"\x00\x01\x02"


def test_decrypt_with_foreign_ciphertext_raises() -> None:
    foreign = Fernet(Fernet.generate_key()).encrypt(b"x")
    with pytest.raises(SecretsDecryptError):
        decrypt(foreign)
