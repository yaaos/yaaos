"""core/secrets — symmetric Fernet wrapper for at-rest application secrets."""

from app.core.secrets.service import (
    SecretsDecryptError,
    decrypt,
    encrypt,
)

__all__ = [
    "SecretsDecryptError",
    "decrypt",
    "encrypt",
]
