"""BYOK service — encrypted per-(org, provider) API keys.

Encryption goes through `core/secrets`. Every mutation emits an audit-log entry
(`byok.set`, `byok.cleared`, `byok.validated`). Plaintext keys cross this
module's surface in only two directions:

- `set(... plaintext)` — caller hands in plaintext; ciphertext is persisted.
- `get(org, provider) -> str | None` — caller receives plaintext, must not log it.
- `validate(org, provider, validator)` — passes plaintext to a caller-supplied
  callable that performs provider-specific verification (e.g. minimal LLM call).

The `validator` pattern keeps `core/byok` free of provider-specific HTTP
logic.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

import structlog
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, audit
from app.core.byok.models import ByokKeyRow
from app.core.database import session as db_session
from app.core.secrets import SecretsDecryptError, decrypt, encrypt

log = structlog.get_logger("core.byok")


class ByokDecryptError(ValueError):
    """Stored ciphertext could not be decrypted with the configured master key."""


# Validator registry — plugins register themselves at bootstrap time so
# `core/byok` stays free of plugin imports. Each entry maps a `provider`
# string to an `async (plaintext: str) -> bool` callable.
_VALIDATORS: dict[str, Callable[[str], Awaitable[bool]]] = {}


def register_validator(provider: str, validator: Callable[[str], Awaitable[bool]]) -> None:
    """Idempotent — re-registering the same provider overwrites. Called from
    plugin `bootstrap()` so a hot-reloaded plugin's validator picks up."""
    _VALIDATORS[provider] = validator


def get_validator(provider: str) -> Callable[[str], Awaitable[bool]] | None:
    return _VALIDATORS.get(provider)


def known_providers() -> list[str]:
    return sorted(_VALIDATORS.keys())


class _ByokAuditPayload(BaseModel):
    provider: str


class _ByokValidatePayload(BaseModel):
    provider: str
    success: bool


async def get(
    org_id: UUID,
    provider: str,
    *,
    session: AsyncSession | None = None,
) -> str | None:
    """Return the decrypted key, or `None` if no row exists. Raises
    `ByokDecryptError` if the row exists but ciphertext is unreadable."""

    async def _query(s: AsyncSession) -> str | None:
        row = (
            await s.execute(
                select(ByokKeyRow).where(ByokKeyRow.org_id == org_id, ByokKeyRow.provider == provider)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        try:
            return decrypt(row.encrypted_value.encode()).decode()
        except SecretsDecryptError as exc:
            log.error("byok.decrypt_failed", org_id=str(org_id), provider=provider)
            raise ByokDecryptError("byok ciphertext unreadable") from exc

    if session is not None:
        return await _query(session)
    async with db_session() as s:
        return await _query(s)


async def set(
    org_id: UUID,
    provider: str,
    plaintext: str,
    *,
    actor: Actor,
    session: AsyncSession | None = None,
) -> None:
    """Upsert the encrypted key. Emits `byok.set` audit entry."""
    if not plaintext:
        raise ValueError("plaintext key must be non-empty")
    ciphertext = encrypt(plaintext).decode()

    async def _write(s: AsyncSession) -> None:
        row = (
            await s.execute(
                select(ByokKeyRow).where(ByokKeyRow.org_id == org_id, ByokKeyRow.provider == provider)
            )
        ).scalar_one_or_none()
        if row is None:
            row = ByokKeyRow(org_id=org_id, provider=provider, encrypted_value=ciphertext)
            s.add(row)
        else:
            row.encrypted_value = ciphertext
        await s.flush()
        await audit(
            "org",
            org_id,
            "byok.set",
            _ByokAuditPayload(provider=provider),
            actor,
            org_id=org_id,
            session=s,
        )

    if session is not None:
        await _write(session)
    else:
        async with db_session() as s:
            await _write(s)
            await s.commit()


async def clear(
    org_id: UUID,
    provider: str,
    *,
    actor: Actor,
    session: AsyncSession | None = None,
) -> bool:
    """Remove the row. Returns True if a row was removed, False if no-op.
    Emits `byok.cleared` audit entry only when a row was removed."""

    async def _delete(s: AsyncSession) -> bool:
        result = await s.execute(
            delete(ByokKeyRow).where(ByokKeyRow.org_id == org_id, ByokKeyRow.provider == provider)
        )
        removed = bool(result.rowcount)
        if removed:
            await audit(
                "org",
                org_id,
                "byok.cleared",
                _ByokAuditPayload(provider=provider),
                actor,
                org_id=org_id,
                session=s,
            )
        return removed

    if session is not None:
        return await _delete(session)
    async with db_session() as s:
        removed = await _delete(s)
        await s.commit()
        return removed


async def validate(
    org_id: UUID,
    provider: str,
    validator: Callable[[str], Awaitable[bool]],
    *,
    actor: Actor,
    session: AsyncSession | None = None,
) -> bool:
    """Decrypt the key, hand it to `validator` (a provider-supplied callable),
    stamp `last_validated_at` on success, and emit `byok.validated` audit entry.
    Returns False if no key is stored OR the validator returned False."""

    async def _run(s: AsyncSession) -> bool:
        row = (
            await s.execute(
                select(ByokKeyRow).where(ByokKeyRow.org_id == org_id, ByokKeyRow.provider == provider)
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        try:
            plaintext = decrypt(row.encrypted_value.encode()).decode()
        except SecretsDecryptError as exc:
            log.error("byok.decrypt_failed", org_id=str(org_id), provider=provider)
            raise ByokDecryptError("byok ciphertext unreadable") from exc
        ok = await validator(plaintext)
        if ok:
            row.last_validated_at = datetime.now(UTC)
            await s.flush()
        await audit(
            "org",
            org_id,
            "byok.validated",
            _ByokValidatePayload(provider=provider, success=ok),
            actor,
            org_id=org_id,
            session=s,
        )
        return ok

    if session is not None:
        return await _run(session)
    async with db_session() as s:
        result = await _run(s)
        await s.commit()
        return result
