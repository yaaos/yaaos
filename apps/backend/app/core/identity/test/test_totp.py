"""TOTP enroll + verify lifecycle tests."""

from __future__ import annotations

import pyotp
import pytest

from app.core.identity.repository import get_totp_secret, insert_user
from app.core.identity.totp import enroll, has_verified_totp, verify


@pytest.mark.asyncio
async def test_enroll_returns_seed_and_otpauth_uri(db_session) -> None:
    user = await insert_user(db_session)
    seed, uri = await enroll(db_session, user_id=user.id)
    assert seed
    assert uri.startswith("otpauth://totp/")
    assert "yaaos" in uri  # issuer

    # Row exists; verified_at is None until verify succeeds.
    row = await get_totp_secret(db_session, user.id)
    assert row is not None and row.verified_at is None
    assert await has_verified_totp(db_session, user.id) is False


@pytest.mark.asyncio
async def test_verify_with_current_code_succeeds(db_session) -> None:
    user = await insert_user(db_session)
    seed, _ = await enroll(db_session, user_id=user.id)
    current = pyotp.TOTP(seed).now()
    ok = await verify(db_session, user_id=user.id, code=current)
    assert ok is True
    assert await has_verified_totp(db_session, user.id) is True


@pytest.mark.asyncio
async def test_verify_with_wrong_code_fails(db_session) -> None:
    user = await insert_user(db_session)
    await enroll(db_session, user_id=user.id)
    ok = await verify(db_session, user_id=user.id, code="000000")
    assert ok is False
    assert await has_verified_totp(db_session, user.id) is False


@pytest.mark.asyncio
async def test_verify_without_secret_fails(db_session) -> None:
    user = await insert_user(db_session)
    ok = await verify(db_session, user_id=user.id, code="123456")
    assert ok is False


@pytest.mark.asyncio
async def test_enroll_replaces_unverified_secret(db_session) -> None:
    user = await insert_user(db_session)
    s1, _ = await enroll(db_session, user_id=user.id)
    s2, _ = await enroll(db_session, user_id=user.id)
    assert s1 != s2  # fresh secret on re-enroll


@pytest.mark.asyncio
async def test_secret_is_encrypted_at_rest(db_session) -> None:
    user = await insert_user(db_session)
    seed, _ = await enroll(db_session, user_id=user.id)
    row = await get_totp_secret(db_session, user.id)
    # The stored bytes must not equal the plaintext seed.
    assert row is not None
    assert seed.encode() not in row.encrypted_secret
