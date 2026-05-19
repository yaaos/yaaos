"""TOTP enrollment + verification + step-up login.

Secrets are Fernet-encrypted with the `yaaos_totp_master_key` env var (a
URL-safe base64 32-byte key, separate from `yaaos_encryption_key` so TOTP
rotation can happen independently of plugin-credential rotation). Plaintext
secrets live only on the wire — once `enroll` returns the QR seed, the row
holds ciphertext forever.

Verification advances `verified_at` once a code matches. Step-up login
asks for a fresh code when the user has a verified secret AND the IdP
didn't satisfy MFA on its own (`amr_satisfied=False`).
"""

from __future__ import annotations

import base64
import secrets
from datetime import UTC, datetime
from uuid import UUID

import pyotp
import structlog
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.domain.identity import repository as repo

log = structlog.get_logger("identity.totp")


class TotpError(ValueError):
    """Code rejected or secret missing."""


def _fernet() -> Fernet:
    """Build a Fernet from `yaaos_totp_master_key`. Falls back to the global
    `yaaos_encryption_key` in non-prod so dev/test stacks need only one
    key. Production deployments are expected to set the dedicated TOTP key."""
    s = get_settings()
    key = s.yaaos_totp_master_key or s.yaaos_encryption_key
    return Fernet(key.encode())


def _new_seed() -> str:
    """32 random bytes encoded as base32 (pyotp's expected format)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


async def enroll(
    session: AsyncSession,
    *,
    user_id: UUID,
    issuer: str = "yaaos",
    account_label: str | None = None,
) -> tuple[str, str]:
    """Mint a fresh secret + persist it (unverified) + return `(seed, otpauth_uri)`.

    The URI is the standard `otpauth://totp/...` format; the SPA renders it
    as a QR code. The raw seed is returned alongside so users on devices
    without a camera can type it manually. After enrollment, `verify` must
    be called with a current code before the row's `verified_at` is set.
    """
    seed = _new_seed()
    ciphertext = _fernet().encrypt(seed.encode())
    await repo.upsert_totp_secret(session, user_id=user_id, encrypted_secret=ciphertext)
    label = account_label or str(user_id)
    uri = pyotp.totp.TOTP(seed).provisioning_uri(name=label, issuer_name=issuer)
    return seed, uri


async def verify(
    session: AsyncSession,
    *,
    user_id: UUID,
    code: str,
) -> bool:
    """Verify `code` against the user's stored TOTP secret. On success,
    stamp `verified_at` + `last_used_at` and return True. On failure return
    False (no row mutation)."""
    row = await repo.get_totp_secret(session, user_id)
    if row is None:
        return False
    try:
        seed = _fernet().decrypt(row.encrypted_secret).decode()
    except InvalidToken:
        log.error("identity.totp.bad_ciphertext", user_id=str(user_id))
        return False
    totp = pyotp.totp.TOTP(seed)
    if not totp.verify(code, valid_window=1):
        return False
    now = datetime.now(UTC)
    if row.verified_at is None:
        row.verified_at = now
    row.last_used_at = now
    await session.flush()
    return True


async def has_verified_totp(session: AsyncSession, user_id: UUID) -> bool:
    row = await repo.get_totp_secret(session, user_id)
    return row is not None and row.verified_at is not None


async def can_be_sso_exempt_owner(session: AsyncSession, user_id: UUID) -> bool:
    """Pre-flight check Phase 12's SSO-config UI calls before letting an
    Owner be picked as the break-glass exempt. The rule: an exempt Owner
    must already have a verified TOTP secret so they can authenticate
    without SSO."""
    return await has_verified_totp(session, user_id)


__all__ = [
    "TotpError",
    "can_be_sso_exempt_owner",
    "enroll",
    "has_verified_totp",
    "verify",
]
