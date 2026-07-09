"""api_keys service — encrypted per-(org, provider) API keys.

Encryption goes through `core/secrets`. Every mutation emits an audit-log entry
(`api_key.set`, `api_key.cleared`, `api_key.validated`). Plaintext keys cross this
module's surface in only two directions:

- `set(... plaintext)` — caller hands in plaintext; ciphertext is persisted.
- `get(org, provider) -> str | None` — caller receives plaintext, must not log it.
- `validate(org, provider, validator)` — passes plaintext to a caller-supplied
  callable that performs provider-specific verification (e.g. minimal LLM call).

The `validator` pattern keeps `core/api_keys` free of provider-specific HTTP
logic.

Required-session: every transactional function takes `session: AsyncSession`
from its caller; never commits. See `apps/backend/docs/patterns.md` §
Session management + atomicity.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

import structlog
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_keys.models import ApiKeyRow
from app.core.audit_log import Actor, audit
from app.core.secrets import SecretsDecryptError, decrypt, encrypt

log = structlog.get_logger("core.api_keys")


class ApiKeyDecryptError(ValueError):
    """Stored ciphertext could not be decrypted with the configured master key."""


class ApiKey(BaseModel):
    """Read-only value object representing one org/provider key entry.
    Plaintext is never included — this carries only metadata."""

    org_id: UUID
    provider: str
    last_validated_at: datetime | None = None
    last_used_at: datetime | None = None
    updated_at: datetime | None = None
    created_at: datetime | None = None


# Validator registry — plugins register themselves at bootstrap time so
# `core/api_keys` stays free of plugin imports. Each entry maps a `provider`
# string to an `async (plaintext: str) -> bool` callable.
_VALIDATORS: dict[str, Callable[[str], Awaitable[bool]]] = {}

# On-change callbacks — called after every successful `set` or `clear`.
# Signature: async (org_id: UUID, *, session: AsyncSession) -> None.
# Registered by consumers (e.g. `core/coding_agent`) that need to fan-out
# a ConfigUpdate whenever a key changes. Session is the caller's transaction
# so the fan-out enqueue is atomic with the api_key mutation.
_OnChangeCb = Callable[..., Awaitable[None]]
_ON_CHANGE: list[_OnChangeCb] = []


def register_on_change(cb: _OnChangeCb) -> None:
    """Register a callback invoked after every successful api_keys `set` or `clear`.

    Callback signature: ``async (org_id: UUID, *, session: AsyncSession) -> None``.
    Idempotent — re-registering the same callable is a no-op.
    """
    if cb not in _ON_CHANGE:
        _ON_CHANGE.append(cb)


def register_validator(provider: str, validator: Callable[[str], Awaitable[bool]]) -> None:
    """Idempotent — re-registering the same provider overwrites. Called from
    plugin `bootstrap()` so a hot-reloaded plugin's validator picks up."""
    _VALIDATORS[provider] = validator


def get_validator(provider: str) -> Callable[[str], Awaitable[bool]] | None:
    return _VALIDATORS.get(provider)


def known_providers() -> list[str]:
    return sorted(_VALIDATORS.keys())


class _ApiKeyAuditPayload(BaseModel):
    provider: str


class _ApiKeyValidatePayload(BaseModel):
    provider: str
    success: bool


async def get(
    org_id: UUID,
    provider: str,
    *,
    session: AsyncSession,
) -> str | None:
    """Return the decrypted key, or `None` if no row exists. Raises
    `ApiKeyDecryptError` if the row exists but ciphertext is unreadable."""
    row = (
        await session.execute(
            select(ApiKeyRow).where(ApiKeyRow.org_id == org_id, ApiKeyRow.provider == provider)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    try:
        return decrypt(row.encrypted_value.encode()).decode()
    except SecretsDecryptError as exc:
        log.error("api_key.decrypt_failed", org_id=str(org_id), provider=provider)
        raise ApiKeyDecryptError("api_key ciphertext unreadable") from exc


async def set(
    org_id: UUID,
    provider: str,
    plaintext: str,
    *,
    actor: Actor,
    session: AsyncSession,
) -> None:
    """Upsert the encrypted key. Emits `api_key.set` audit entry."""
    if not plaintext:
        raise ValueError("plaintext key must be non-empty")
    ciphertext = encrypt(plaintext).decode()
    row = (
        await session.execute(
            select(ApiKeyRow).where(ApiKeyRow.org_id == org_id, ApiKeyRow.provider == provider)
        )
    ).scalar_one_or_none()
    if row is None:
        row = ApiKeyRow(org_id=org_id, provider=provider, encrypted_value=ciphertext)
        session.add(row)
    else:
        row.encrypted_value = ciphertext
    await session.flush()
    await audit(
        "org",
        org_id,
        "api_key.set",
        _ApiKeyAuditPayload(provider=provider),
        actor,
        org_id=org_id,
        session=session,
    )
    for cb in _ON_CHANGE:
        await cb(org_id, session=session)


async def clear(
    org_id: UUID,
    provider: str,
    *,
    actor: Actor,
    session: AsyncSession,
) -> bool:
    """Remove the row. Returns True if a row was removed, False if no-op.
    Emits `api_key.cleared` audit entry only when a row was removed."""
    result = await session.execute(
        delete(ApiKeyRow).where(ApiKeyRow.org_id == org_id, ApiKeyRow.provider == provider)
    )
    removed = bool(result.rowcount)
    if removed:
        await audit(
            "org",
            org_id,
            "api_key.cleared",
            _ApiKeyAuditPayload(provider=provider),
            actor,
            org_id=org_id,
            session=session,
        )
        for cb in _ON_CHANGE:
            await cb(org_id, session=session)
    return removed


async def validate(
    org_id: UUID,
    provider: str,
    validator: Callable[[str], Awaitable[bool]],
    *,
    actor: Actor,
    session: AsyncSession,
) -> bool:
    """Decrypt the key, hand it to `validator` (a provider-supplied callable),
    stamp `last_validated_at` on success, and emit `api_key.validated` audit entry.
    Returns False if no key is stored OR the validator returned False."""
    row = (
        await session.execute(
            select(ApiKeyRow).where(ApiKeyRow.org_id == org_id, ApiKeyRow.provider == provider)
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    try:
        plaintext = decrypt(row.encrypted_value.encode()).decode()
    except SecretsDecryptError as exc:
        log.error("api_key.decrypt_failed", org_id=str(org_id), provider=provider)
        raise ApiKeyDecryptError("api_key ciphertext unreadable") from exc
    ok = await validator(plaintext)
    if ok:
        row.last_validated_at = datetime.now(UTC)
        await session.flush()
    await audit(
        "org",
        org_id,
        "api_key.validated",
        _ApiKeyValidatePayload(provider=provider, success=ok),
        actor,
        org_id=org_id,
        session=session,
    )
    return ok


async def list_keys_for_org(
    org_id: UUID,
    *,
    session: AsyncSession,
) -> list[ApiKey]:
    """Return metadata for all stored keys belonging to `org_id`.

    No plaintext crosses this boundary — callers receive `ApiKey` value objects.
    """
    rows = (await session.execute(select(ApiKeyRow).where(ApiKeyRow.org_id == org_id))).scalars().all()
    return [
        ApiKey(
            org_id=row.org_id,
            provider=row.provider,
            last_validated_at=row.last_validated_at,
            last_used_at=row.last_used_at,
            updated_at=row.updated_at,
            created_at=row.created_at,
        )
        for row in rows
    ]
