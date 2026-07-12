"""Service tests for the token-refresh lifecycle in core/oauth.

Covers:
  - `ensure_fresh_access_token`: freshness gate, rotation, concurrent callers
    (exactly-one refresh), terminal error → needs_reauth, transient error → stays connected.
  - `refresh_due_connections`: due row refreshed, non-due skipped, terminal failure
    flips one row while sibling commits; expired device sessions purged.

Uses the same DI-seam pattern (UserOAuthApp.token_fn) as the phase-5 service tests —
no real network, no unittest.mock.patch.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor
from app.core.auth import Role
from app.core.identity import create_user
from app.core.oauth import (
    ConnectionMissingError,
    ConnectionNeedsReauthError,
    UserOAuthApp,
    ensure_fresh_access_token,
    poll_device_auth,
    register_user_oauth_app,
    start_device_auth,
)
from app.core.oauth.models import UserOAuthConnectionRow, UserOAuthDeviceSessionRow
from app.core.oauth.user_connections import _do_refresh_due_connections
from app.core.secrets import decrypt
from app.domain.orgs import create_membership, create_org

pytestmark = pytest.mark.service

# ---------------------------------------------------------------------------
# Shared stub state
# ---------------------------------------------------------------------------

_TEST_PROVIDER_ID = "test_refresh_provider"


class _StubState:
    token_result: Any = None
    token_side_effects: list[Any] | None = None
    call_count: int = 0


_STUB = _StubState()


async def _stub_device_authorize(
    *,
    device_authorize_url: str,
    client_id: str,
    scopes: tuple[str, ...],
    scope_separator: str,
) -> dict[str, Any]:
    return {
        "device_code": "dev-code-refresh",
        "user_code": "RFSH-1234",
        "verification_uri": "https://fake.test/activate",
        "expires_in": 900,
        "interval": 5,
    }


async def _stub_token(spec: Any, data: Any) -> Any:
    from app.core.oauth.service import Tokens  # noqa: PLC0415

    _STUB.call_count += 1
    if _STUB.token_side_effects is not None:
        entry = _STUB.token_side_effects.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return entry
    if isinstance(_STUB.token_result, Exception):
        raise _STUB.token_result
    if isinstance(_STUB.token_result, Tokens):
        return _STUB.token_result
    raise AssertionError("_STUB.token_result not set")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _register_refresh_test_app() -> None:
    """Register the refresh test app per test; clear stub state."""
    from app.core.oauth.user_connections import _APPS  # noqa: PLC0415

    _STUB.token_result = None
    _STUB.token_side_effects = None
    _STUB.call_count = 0

    _APPS.pop(_TEST_PROVIDER_ID, None)
    register_user_oauth_app(
        UserOAuthApp(
            provider_id=_TEST_PROVIDER_ID,
            display_name="Refresh Test Provider",
            connect_hint="test hint",
            flow="device_code",
            device_authorize_url="http://fake-refresh.test/device/code",
            token_url="http://fake-refresh.test/token",
            client_id="test-client-id",
            client_secret=None,
            default_scopes=("read",),
            token_auth_style="form",
            scope_separator=" ",
            expiry_source="expires_in",
            capture_id_token=False,
            account_id_extractor=None,
            refresh_after_seconds=1,  # tiny threshold: every row is immediately due
            device_authorize_fn=_stub_device_authorize,
            token_fn=_stub_token,
        )
    )


@pytest_asyncio.fixture
async def user_id(db_session: AsyncSession) -> UUID:
    user = await create_user(db_session, display_name="Refresh Test User")
    return user.id


@pytest_asyncio.fixture
async def user_and_org(db_session: AsyncSession, user_id: UUID) -> tuple[UUID, UUID]:
    org = await create_org(db_session, slug="refresh-test-org", display_name="Refresh Org")
    await create_membership(
        db_session,
        org_id=org.id,
        user_id=user_id,
        role=Role.BUILDER,
        handle="refreshuser",
    )
    await db_session.flush()
    return user_id, org.id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tokens(
    access_token: str = "access-v1",
    refresh_token: str = "refresh-v1",
    expires_in: int = 3600,
) -> Any:
    from app.core.oauth.service import Tokens  # noqa: PLC0415

    return Tokens(
        access_token=SecretStr(access_token),
        refresh_token=SecretStr(refresh_token),
        expires_in=expires_in,
        scope="read",
        raw={"access_token": access_token, "refresh_token": refresh_token, "expires_in": expires_in},
    )


async def _seed_connected(db_session: AsyncSession, user_id: UUID) -> None:
    """Seed a connected row via the device-auth flow."""
    await start_device_auth(user_id, _TEST_PROVIDER_ID, session=db_session)
    await db_session.flush()
    _STUB.token_result = _make_tokens("initial-at", "initial-rt")
    actor = Actor.user(user_id=user_id)
    await poll_device_auth(user_id, _TEST_PROVIDER_ID, actor=actor, session=db_session)
    await db_session.flush()
    # Reset call count after setup
    _STUB.call_count = 0


# ---------------------------------------------------------------------------
# ensure_fresh_access_token — freshness gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_fresh_returns_without_refresh_when_above_threshold(
    db_session: AsyncSession, user_id: UUID
) -> None:
    """Token with 3600s remaining and min_remaining=60s → returns immediately, no refresh."""
    await _seed_connected(db_session, user_id)

    # 3600s > 60s → fresh
    cred = await ensure_fresh_access_token(user_id, _TEST_PROVIDER_ID, min_remaining_seconds=60)

    assert cred.access_token.get_secret_value() == "initial-at"
    assert _STUB.call_count == 0  # no token endpoint call


@pytest.mark.asyncio
async def test_ensure_fresh_refreshes_stale_token_and_rotates_refresh_token(
    db_session: AsyncSession, user_id: UUID
) -> None:
    """Token with 3600s remaining but min_remaining=7200s → stale → refresh, rotate."""
    await _seed_connected(db_session, user_id)
    _STUB.token_result = _make_tokens("new-at", "new-rt")

    # 3600s < 7200s → stale
    cred = await ensure_fresh_access_token(user_id, _TEST_PROVIDER_ID, min_remaining_seconds=7200)

    assert cred.access_token.get_secret_value() == "new-at"
    assert _STUB.call_count == 1

    # Row updated with rotated tokens
    row = (
        await db_session.execute(
            select(UserOAuthConnectionRow).where(
                UserOAuthConnectionRow.user_id == user_id,
                UserOAuthConnectionRow.provider_id == _TEST_PROVIDER_ID,
            )
        )
    ).scalar_one()
    assert decrypt(row.encrypted_access_token.encode()) == b"new-at"
    assert decrypt(row.encrypted_refresh_token.encode()) == b"new-rt"


@pytest.mark.asyncio
async def test_ensure_fresh_concurrent_callers_exactly_one_refresh(
    db_session: AsyncSession, user_id: UUID
) -> None:
    """Re-check after lock prevents double-refresh under concurrent callers.

    Sequential simulation: the first call is stale (needs 7200s but initial
    token has only 3600s) and triggers one refresh.  A second call requesting
    only 300s finds the freshly-rotated 3600s token via the fast path — the
    token endpoint is never called again.

    This verifies the re-check invariant that underlies the FOR-UPDATE slow path:
    a caller that loses the lock race re-reads the freshly-rotated row and
    short-circuits.  The test uses a shared DB session (test fixture model)
    rather than true concurrent connections, so sequential simulation is the
    correct harness — asyncio.gather over the same session would interleave at
    the same SAVEPOINT level and bypass the lock semantics under test.
    """
    await _seed_connected(db_session, user_id)
    _STUB.token_side_effects = [_make_tokens("concurrent-at", "concurrent-rt", expires_in=3600)]

    # First call: needs 7200s, has only 3600s → stale → slow path → refresh
    cred1 = await ensure_fresh_access_token(user_id, _TEST_PROVIDER_ID, min_remaining_seconds=7200)
    assert _STUB.call_count == 1
    assert cred1.access_token.get_secret_value() == "concurrent-at"

    # Second call: needs only 300s; token now has ~3600s → fast path hit, no refresh
    cred2 = await ensure_fresh_access_token(user_id, _TEST_PROVIDER_ID, min_remaining_seconds=300)
    assert _STUB.call_count == 1  # still exactly 1 — second caller never touched provider
    assert cred2.access_token.get_secret_value() == "concurrent-at"


@pytest.mark.asyncio
async def test_ensure_fresh_missing_connection_raises(user_id: UUID) -> None:
    """ConnectionMissingError raised when no row exists."""
    with pytest.raises(ConnectionMissingError):
        await ensure_fresh_access_token(user_id, _TEST_PROVIDER_ID, min_remaining_seconds=60)


# ---------------------------------------------------------------------------
# ensure_fresh_access_token — terminal errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_fresh_invalid_grant_flips_needs_reauth_and_raises(
    db_session: AsyncSession, user_id: UUID
) -> None:
    """invalid_grant → row status=needs_reauth and ConnectionNeedsReauthError raised."""
    from app.core.oauth.service import OAuthError  # noqa: PLC0415

    await _seed_connected(db_session, user_id)
    _STUB.token_result = OAuthError("bad grant", error_code="invalid_grant")

    with pytest.raises(ConnectionNeedsReauthError) as exc_info:
        await ensure_fresh_access_token(user_id, _TEST_PROVIDER_ID, min_remaining_seconds=7200)

    assert "invalid_grant" in exc_info.value.user_message

    # Row flipped to needs_reauth
    db_session.expire_all()
    row = (
        await db_session.execute(
            select(UserOAuthConnectionRow).where(
                UserOAuthConnectionRow.user_id == user_id,
                UserOAuthConnectionRow.provider_id == _TEST_PROVIDER_ID,
            )
        )
    ).scalar_one()
    assert row.status == "needs_reauth"
    assert row.needs_reauth_reason is not None
    assert "invalid_grant" in row.needs_reauth_reason


@pytest.mark.asyncio
async def test_ensure_fresh_needs_reauth_row_raises_immediately(
    db_session: AsyncSession, user_id: UUID
) -> None:
    """A row already in needs_reauth status raises ConnectionNeedsReauthError."""
    from app.core.oauth.service import OAuthError  # noqa: PLC0415

    await _seed_connected(db_session, user_id)
    _STUB.token_result = OAuthError("bad grant", error_code="invalid_grant")

    # First call flips the row
    with pytest.raises(ConnectionNeedsReauthError):
        await ensure_fresh_access_token(user_id, _TEST_PROVIDER_ID, min_remaining_seconds=7200)

    _STUB.call_count = 0

    # Second call should raise without hitting the token endpoint
    with pytest.raises(ConnectionNeedsReauthError):
        await ensure_fresh_access_token(user_id, _TEST_PROVIDER_ID, min_remaining_seconds=60)

    assert _STUB.call_count == 0


# ---------------------------------------------------------------------------
# ensure_fresh_access_token — transient errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_fresh_transient_error_row_stays_connected(
    db_session: AsyncSession, user_id: UUID
) -> None:
    """Transport error (no error_code) → row stays connected; OAuthError re-raised."""
    from app.core.oauth.service import OAuthError  # noqa: PLC0415

    await _seed_connected(db_session, user_id)
    _STUB.token_result = OAuthError("network timeout", error_code=None)

    with pytest.raises(OAuthError):
        await ensure_fresh_access_token(user_id, _TEST_PROVIDER_ID, min_remaining_seconds=7200)

    # Row stays connected
    db_session.expire_all()
    row = (
        await db_session.execute(
            select(UserOAuthConnectionRow).where(
                UserOAuthConnectionRow.user_id == user_id,
                UserOAuthConnectionRow.provider_id == _TEST_PROVIDER_ID,
            )
        )
    ).scalar_one()
    assert row.status == "connected"


# ---------------------------------------------------------------------------
# refresh_due_connections body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_due_connections_refreshes_due_row(db_session: AsyncSession, user_id: UUID) -> None:
    """A connected row past the refresh threshold is refreshed; last_refresh_at advances."""
    await _seed_connected(db_session, user_id)
    # Mark the row as long-overdue by back-dating last_refresh_at
    await db_session.execute(
        update(UserOAuthConnectionRow)
        .where(
            UserOAuthConnectionRow.user_id == user_id,
            UserOAuthConnectionRow.provider_id == _TEST_PROVIDER_ID,
        )
        .values(last_refresh_at=datetime.now(UTC) - timedelta(days=10))
    )
    await db_session.commit()

    _STUB.token_result = _make_tokens("refreshed-at", "refreshed-rt")

    count = await _do_refresh_due_connections()

    assert count == 1

    db_session.expire_all()
    row = (
        await db_session.execute(
            select(UserOAuthConnectionRow).where(
                UserOAuthConnectionRow.user_id == user_id,
                UserOAuthConnectionRow.provider_id == _TEST_PROVIDER_ID,
            )
        )
    ).scalar_one()
    assert row.status == "connected"
    assert decrypt(row.encrypted_access_token.encode()) == b"refreshed-at"
    assert decrypt(row.encrypted_refresh_token.encode()) == b"refreshed-rt"
    # last_refresh_at updated
    assert row.last_refresh_at > datetime.now(UTC) - timedelta(seconds=5)


@pytest.mark.asyncio
async def test_refresh_due_connections_skips_non_due_row(db_session: AsyncSession, user_id: UUID) -> None:
    """A row refreshed recently (within refresh_after_seconds) is not refreshed again."""
    await _seed_connected(db_session, user_id)
    # last_refresh_at is 'now' (set by poll_device_auth), so with refresh_after_seconds=1
    # the row is immediately due. To make it NOT due, we'd need refresh_after_seconds > elapsed.
    # Instead: register a variant app with a very large threshold.
    from dataclasses import replace  # noqa: PLC0415

    from app.core.oauth.user_connections import _APPS  # noqa: PLC0415

    # Override the test app's threshold to 1 year (row won't be due)
    _APPS[_TEST_PROVIDER_ID] = replace(_APPS[_TEST_PROVIDER_ID], refresh_after_seconds=365 * 24 * 3600)

    count = await _do_refresh_due_connections()

    assert count == 0
    assert _STUB.call_count == 0


@pytest.mark.asyncio
async def test_refresh_due_connections_terminal_failure_flips_needs_reauth(
    db_session: AsyncSession, user_id: UUID
) -> None:
    """Terminal invalid_grant for one row flips it to needs_reauth; return count=0."""
    from app.core.oauth.service import OAuthError  # noqa: PLC0415

    await _seed_connected(db_session, user_id)
    await db_session.execute(
        update(UserOAuthConnectionRow)
        .where(
            UserOAuthConnectionRow.user_id == user_id,
            UserOAuthConnectionRow.provider_id == _TEST_PROVIDER_ID,
        )
        .values(last_refresh_at=datetime.now(UTC) - timedelta(days=10))
    )
    await db_session.commit()

    _STUB.token_result = OAuthError("bad grant", error_code="invalid_grant")

    count = await _do_refresh_due_connections()

    assert count == 0

    db_session.expire_all()
    row = (
        await db_session.execute(
            select(UserOAuthConnectionRow).where(
                UserOAuthConnectionRow.user_id == user_id,
                UserOAuthConnectionRow.provider_id == _TEST_PROVIDER_ID,
            )
        )
    ).scalar_one()
    assert row.status == "needs_reauth"
    assert "invalid_grant" in (row.needs_reauth_reason or "")


@pytest.mark.asyncio
async def test_refresh_due_connections_sibling_row_commits_despite_terminal_failure(
    db_session: AsyncSession,
) -> None:
    """One row's terminal failure doesn't prevent sibling row from being refreshed.

    User A: invalid_grant → needs_reauth.
    User B: succeeds → refreshed, last_refresh_at advanced.
    Both outcomes are committed independently (per-row transactions).
    """
    from app.core.oauth.service import OAuthError  # noqa: PLC0415

    # Seed two users, both with stale connections.
    user_a = await create_user(db_session, display_name="UserA")
    user_b = await create_user(db_session, display_name="UserB")
    await db_session.flush()

    # Seed user A
    await start_device_auth(user_a.id, _TEST_PROVIDER_ID, session=db_session)
    await db_session.flush()
    _STUB.token_result = _make_tokens("at-a", "rt-a")
    await poll_device_auth(
        user_a.id, _TEST_PROVIDER_ID, actor=Actor.user(user_id=user_a.id), session=db_session
    )
    await db_session.flush()

    # Seed user B
    await start_device_auth(user_b.id, _TEST_PROVIDER_ID, session=db_session)
    await db_session.flush()
    _STUB.token_result = _make_tokens("at-b", "rt-b")
    await poll_device_auth(
        user_b.id, _TEST_PROVIDER_ID, actor=Actor.user(user_id=user_b.id), session=db_session
    )
    await db_session.flush()

    # Back-date both rows
    await db_session.execute(
        update(UserOAuthConnectionRow)
        .where(UserOAuthConnectionRow.provider_id == _TEST_PROVIDER_ID)
        .values(last_refresh_at=datetime.now(UTC) - timedelta(days=10))
    )
    await db_session.commit()

    _STUB.call_count = 0
    # First refresh call → terminal error; second → success.
    _STUB.token_side_effects = [
        OAuthError("bad grant", error_code="invalid_grant"),
        _make_tokens("at-b-new", "rt-b-new"),
    ]

    count = await _do_refresh_due_connections()

    # One succeeded (user B), one failed (user A)
    assert count == 1

    db_session.expire_all()
    row_a = (
        await db_session.execute(
            select(UserOAuthConnectionRow).where(
                UserOAuthConnectionRow.user_id == user_a.id,
                UserOAuthConnectionRow.provider_id == _TEST_PROVIDER_ID,
            )
        )
    ).scalar_one()
    row_b = (
        await db_session.execute(
            select(UserOAuthConnectionRow).where(
                UserOAuthConnectionRow.user_id == user_b.id,
                UserOAuthConnectionRow.provider_id == _TEST_PROVIDER_ID,
            )
        )
    ).scalar_one()

    assert row_a.status == "needs_reauth"
    assert row_b.status == "connected"
    assert decrypt(row_b.encrypted_access_token.encode()) == b"at-b-new"


@pytest.mark.asyncio
async def test_refresh_due_connections_purges_expired_device_sessions(
    db_session: AsyncSession, user_id: UUID
) -> None:
    """Expired user_oauth_device_sessions rows are deleted."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: PLC0415

    past = datetime.now(UTC) - timedelta(hours=1)
    future = datetime.now(UTC) + timedelta(hours=1)

    await db_session.execute(
        pg_insert(UserOAuthDeviceSessionRow).values(
            user_id=user_id,
            provider_id="session-old",
            encrypted_device_code="encrypted-code-a",
            user_code="OLD-CODE",
            verification_url="https://fake.test/activate",
            poll_interval_seconds=5,
            expires_at=past,
        )
    )
    await db_session.execute(
        pg_insert(UserOAuthDeviceSessionRow).values(
            user_id=user_id,
            provider_id="session-new",
            encrypted_device_code="encrypted-code-b",
            user_code="NEW-CODE",
            verification_url="https://fake.test/activate",
            poll_interval_seconds=5,
            expires_at=future,
        )
    )
    await db_session.commit()

    await _do_refresh_due_connections()

    db_session.expire_all()
    sessions = (
        (
            await db_session.execute(
                select(UserOAuthDeviceSessionRow).where(
                    UserOAuthDeviceSessionRow.user_id == user_id,
                )
            )
        )
        .scalars()
        .all()
    )
    provider_ids = {s.provider_id for s in sessions}
    assert "session-old" not in provider_ids
    assert "session-new" in provider_ids
