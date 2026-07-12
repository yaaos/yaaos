"""Service tests for the core/oauth user-connection subsystem.

Tests the full device-auth connect/disconnect flow with stub HTTP callables
injected via UserOAuthApp.device_authorize_fn / token_fn — no real network
and no unittest.mock.patch.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, list_for_entity
from app.core.auth import Role
from app.core.identity import create_user
from app.core.oauth import (
    DeviceAuthStart,
    UserOAuthApp,
    disconnect_user_connection,
    get_user_connection,
    poll_device_auth,
    register_user_oauth_app,
    start_device_auth,
)
from app.core.oauth.models import UserOAuthConnectionRow, UserOAuthDeviceSessionRow
from app.core.secrets import decrypt
from app.domain.orgs import create_membership, create_org

# ---------------------------------------------------------------------------
# Shared stub state — set per test, read by the injected callables.
# ---------------------------------------------------------------------------

_TEST_PROVIDER_ID = "test_provider_svc"


class _StubState:
    """Mutable state container read by the test stub callables."""

    device_response: dict[str, Any] | None = None
    # token_result is either a Tokens instance (returned) or an OAuthError (raised).
    token_result: Any = None
    # token_side_effects: sequential list; each call pops the first entry.
    token_side_effects: list[Any] | None = None


_STUB = _StubState()


async def _stub_device_authorize(
    *,
    device_authorize_url: str,
    client_id: str,
    scopes: tuple[str, ...],
    scope_separator: str,
) -> dict[str, Any]:
    return _STUB.device_response or {}


async def _stub_token(spec: Any, data: Any) -> Any:  # type matches _post_token
    from app.core.oauth.service import Tokens  # noqa: PLC0415

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
def _register_test_app() -> None:
    """Register (or replace) the test UserOAuthApp per test.

    Each test gets a fresh stub state and a freshly registered app so there's
    no cross-test pollution.
    """
    from app.core.oauth.user_connections import _APPS  # noqa: PLC0415

    # Reset stub state.
    _STUB.device_response = None
    _STUB.token_result = None
    _STUB.token_side_effects = None

    # Clear any previous registration (idempotent across repeated test runs).
    _APPS.pop(_TEST_PROVIDER_ID, None)

    register_user_oauth_app(
        UserOAuthApp(
            provider_id=_TEST_PROVIDER_ID,
            display_name="Test Provider",
            connect_hint="test hint",
            flow="device_code",
            device_authorize_url="http://fake-auth.test/device/code",
            token_url="http://fake-auth.test/token",
            client_id="test-client-id",
            client_secret=None,  # public client
            default_scopes=("read",),
            token_auth_style="form",
            scope_separator=" ",
            expiry_source="expires_in",
            capture_id_token=False,
            account_id_extractor=lambda tokens: "test-account-123",
            device_authorize_fn=_stub_device_authorize,
            token_fn=_stub_token,
        )
    )


@pytest_asyncio.fixture
async def user_id(db_session: AsyncSession) -> UUID:
    """A user row in the test DB."""
    user = await create_user(db_session, display_name="Test User")
    return user.id


@pytest_asyncio.fixture
async def user_and_org(db_session: AsyncSession, user_id: UUID) -> tuple[UUID, UUID]:
    """User with one org membership — needed for audit fan-out assertions."""
    org = await create_org(db_session, slug="test-org-svc", display_name="Test Org")
    await create_membership(
        db_session,
        org_id=org.id,
        user_id=user_id,
        role=Role.BUILDER,
        handle="testuser",
    )
    await db_session.flush()
    return user_id, org.id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _device_auth_response() -> dict[str, Any]:
    return {
        "device_code": "test-device-code-xyz",
        "user_code": "ABCD-1234",
        "verification_uri": "https://fake-auth.test/activate",
        "expires_in": 900,
        "interval": 5,
    }


def _make_tokens(access_token: str = "access-token-abc") -> Any:
    from app.core.oauth.service import Tokens  # noqa: PLC0415

    return Tokens(
        access_token=SecretStr(access_token),
        refresh_token=None,
        expires_in=3600,
        scope="read",
        raw={"access_token": access_token, "expires_in": 3600, "scope": "read"},
    )


# ---------------------------------------------------------------------------
# Tests: start_device_auth
# ---------------------------------------------------------------------------


@pytest.mark.service
@pytest.mark.asyncio
async def test_start_device_auth_upserts_session(db_session: AsyncSession, user_id: UUID) -> None:
    """start_device_auth calls the device-authorize endpoint and upserts a session row."""
    _STUB.device_response = _device_auth_response()

    result = await start_device_auth(user_id, _TEST_PROVIDER_ID, session=db_session)
    await db_session.flush()

    assert isinstance(result, DeviceAuthStart)
    assert result.user_code == "ABCD-1234"
    assert result.verification_url == "https://fake-auth.test/activate"
    assert result.poll_interval_seconds == 5

    row = (
        await db_session.execute(
            select(UserOAuthDeviceSessionRow).where(
                UserOAuthDeviceSessionRow.user_id == user_id,
                UserOAuthDeviceSessionRow.provider_id == _TEST_PROVIDER_ID,
            )
        )
    ).scalar_one()
    assert row.user_code == "ABCD-1234"
    # device_code is encrypted — verify it decrypts back
    assert decrypt(row.encrypted_device_code.encode()) == b"test-device-code-xyz"


@pytest.mark.service
@pytest.mark.asyncio
async def test_start_device_auth_replaces_existing_session(db_session: AsyncSession, user_id: UUID) -> None:
    """Starting again replaces the old session row (upsert, not append)."""
    _STUB.device_side_effects: list[dict[str, Any]] = [
        {**_device_auth_response(), "user_code": "FIRST-CODE"},
        {**_device_auth_response(), "user_code": "SECND-CODE"},
    ]
    # Use side_effects list for sequential calls.
    _STUB.token_side_effects = None

    call_n = 0

    async def _stub_device_seq(*, device_authorize_url, client_id, scopes, scope_separator):  # type: ignore[override]
        nonlocal call_n
        resp = _STUB.device_side_effects[call_n]  # type: ignore[attr-defined]
        call_n += 1
        return resp

    # Patch the stub callable for this test only.
    from app.core.oauth.user_connections import _APPS  # noqa: PLC0415

    old_app = _APPS[_TEST_PROVIDER_ID]
    from dataclasses import replace  # noqa: PLC0415

    _APPS[_TEST_PROVIDER_ID] = replace(old_app, device_authorize_fn=_stub_device_seq)

    await start_device_auth(user_id, _TEST_PROVIDER_ID, session=db_session)
    await db_session.flush()
    await start_device_auth(user_id, _TEST_PROVIDER_ID, session=db_session)
    await db_session.flush()

    rows = (
        (
            await db_session.execute(
                select(UserOAuthDeviceSessionRow).where(
                    UserOAuthDeviceSessionRow.user_id == user_id,
                    UserOAuthDeviceSessionRow.provider_id == _TEST_PROVIDER_ID,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].user_code == "SECND-CODE"


# ---------------------------------------------------------------------------
# Tests: poll_device_auth — pending states
# ---------------------------------------------------------------------------


@pytest.mark.service
@pytest.mark.asyncio
async def test_poll_pending_returns_pending(db_session: AsyncSession, user_id: UUID) -> None:
    """authorization_pending OAuthError → returns 'pending', session unchanged."""
    from app.core.oauth.service import OAuthError  # noqa: PLC0415

    _STUB.device_response = _device_auth_response()
    await start_device_auth(user_id, _TEST_PROVIDER_ID, session=db_session)
    await db_session.flush()

    _STUB.token_result = OAuthError("pending", error_code="authorization_pending")
    actor = Actor.user(user_id=user_id)
    status = await poll_device_auth(user_id, _TEST_PROVIDER_ID, actor=actor, session=db_session)
    assert status == "pending"


@pytest.mark.service
@pytest.mark.asyncio
async def test_poll_slow_down_bumps_interval(db_session: AsyncSession, user_id: UUID) -> None:
    """slow_down OAuthError → returns 'pending' and bumps poll_interval_seconds by 5."""
    from app.core.oauth.service import OAuthError  # noqa: PLC0415

    _STUB.device_response = _device_auth_response()
    await start_device_auth(user_id, _TEST_PROVIDER_ID, session=db_session)
    await db_session.flush()

    _STUB.token_result = OAuthError("slow_down", error_code="slow_down")
    actor = Actor.user(user_id=user_id)
    status = await poll_device_auth(user_id, _TEST_PROVIDER_ID, actor=actor, session=db_session)
    await db_session.flush()

    assert status == "pending"
    # Expire so the next SELECT re-fetches from DB (the upsert bypasses SQLAlchemy's ORM cache).
    db_session.expire_all()
    row = (
        await db_session.execute(
            select(UserOAuthDeviceSessionRow).where(
                UserOAuthDeviceSessionRow.user_id == user_id,
                UserOAuthDeviceSessionRow.provider_id == _TEST_PROVIDER_ID,
            )
        )
    ).scalar_one()
    assert row.poll_interval_seconds == 10  # 5 + 5


# ---------------------------------------------------------------------------
# Tests: poll_device_auth — grant (connected)
# ---------------------------------------------------------------------------


@pytest.mark.service
@pytest.mark.asyncio
async def test_poll_grant_stores_tokens_and_deletes_session(
    db_session: AsyncSession, user_and_org: tuple[UUID, UUID]
) -> None:
    """Grant response: tokens encrypted, session deleted, connection row upserted."""
    user_id, org_id = user_and_org

    _STUB.device_response = _device_auth_response()
    await start_device_auth(user_id, _TEST_PROVIDER_ID, session=db_session)
    await db_session.flush()

    _STUB.token_result = _make_tokens()
    actor = Actor.user(user_id=user_id)
    status = await poll_device_auth(user_id, _TEST_PROVIDER_ID, actor=actor, session=db_session)
    await db_session.flush()

    assert status == "connected"

    # Session row deleted.
    session_row = (
        await db_session.execute(
            select(UserOAuthDeviceSessionRow).where(
                UserOAuthDeviceSessionRow.user_id == user_id,
                UserOAuthDeviceSessionRow.provider_id == _TEST_PROVIDER_ID,
            )
        )
    ).scalar_one_or_none()
    assert session_row is None

    # Connection row created with encrypted token.
    conn_row = (
        await db_session.execute(
            select(UserOAuthConnectionRow).where(
                UserOAuthConnectionRow.user_id == user_id,
                UserOAuthConnectionRow.provider_id == _TEST_PROVIDER_ID,
            )
        )
    ).scalar_one()
    assert conn_row.status == "connected"
    assert conn_row.external_account_id == "test-account-123"
    assert decrypt(conn_row.encrypted_access_token.encode()) == b"access-token-abc"

    # Audit rows: one per org membership.
    audit_rows = await list_for_entity("user", user_id, org_id=org_id, kinds=["oauth_connection.connected"])
    assert len(audit_rows) == 1


@pytest.mark.service
@pytest.mark.asyncio
async def test_poll_access_denied_deletes_session(db_session: AsyncSession, user_id: UUID) -> None:
    """access_denied → returns 'denied', session deleted."""
    from app.core.oauth.service import OAuthError  # noqa: PLC0415

    _STUB.device_response = _device_auth_response()
    await start_device_auth(user_id, _TEST_PROVIDER_ID, session=db_session)
    await db_session.flush()

    _STUB.token_result = OAuthError("denied", error_code="access_denied")
    actor = Actor.user(user_id=user_id)
    status = await poll_device_auth(user_id, _TEST_PROVIDER_ID, actor=actor, session=db_session)
    await db_session.flush()

    assert status == "denied"
    session_row = (
        await db_session.execute(
            select(UserOAuthDeviceSessionRow).where(
                UserOAuthDeviceSessionRow.user_id == user_id,
                UserOAuthDeviceSessionRow.provider_id == _TEST_PROVIDER_ID,
            )
        )
    ).scalar_one_or_none()
    assert session_row is None


@pytest.mark.service
@pytest.mark.asyncio
async def test_poll_expired_token_deletes_session(db_session: AsyncSession, user_id: UUID) -> None:
    """expired_token → returns 'expired', session deleted."""
    from app.core.oauth.service import OAuthError  # noqa: PLC0415

    _STUB.device_response = _device_auth_response()
    await start_device_auth(user_id, _TEST_PROVIDER_ID, session=db_session)
    await db_session.flush()

    _STUB.token_result = OAuthError("expired", error_code="expired_token")
    actor = Actor.user(user_id=user_id)
    status = await poll_device_auth(user_id, _TEST_PROVIDER_ID, actor=actor, session=db_session)
    await db_session.flush()

    assert status == "expired"
    row = (
        await db_session.execute(
            select(UserOAuthDeviceSessionRow).where(
                UserOAuthDeviceSessionRow.user_id == user_id,
                UserOAuthDeviceSessionRow.provider_id == _TEST_PROVIDER_ID,
            )
        )
    ).scalar_one_or_none()
    assert row is None


@pytest.mark.service
@pytest.mark.asyncio
async def test_poll_with_no_session_returns_none(db_session: AsyncSession, user_id: UUID) -> None:
    """With no session row, poll returns 'none' immediately."""
    actor = Actor.user(user_id=user_id)
    status = await poll_device_auth(user_id, _TEST_PROVIDER_ID, actor=actor, session=db_session)
    assert status == "none"


# ---------------------------------------------------------------------------
# Tests: get_user_connection
# ---------------------------------------------------------------------------


@pytest.mark.service
@pytest.mark.asyncio
async def test_get_user_connection_returns_none_when_absent(db_session: AsyncSession, user_id: UUID) -> None:
    conn = await get_user_connection(user_id, _TEST_PROVIDER_ID, session=db_session)
    assert conn is None


@pytest.mark.service
@pytest.mark.asyncio
async def test_get_user_connection_returns_view_after_connect(
    db_session: AsyncSession, user_and_org: tuple[UUID, UUID]
) -> None:
    """After a successful grant, get_user_connection returns the connected VO."""
    user_id, _ = user_and_org

    _STUB.device_response = _device_auth_response()
    await start_device_auth(user_id, _TEST_PROVIDER_ID, session=db_session)
    await db_session.flush()

    _STUB.token_result = _make_tokens("at")
    actor = Actor.user(user_id=user_id)
    await poll_device_auth(user_id, _TEST_PROVIDER_ID, actor=actor, session=db_session)
    await db_session.flush()

    conn = await get_user_connection(user_id, _TEST_PROVIDER_ID, session=db_session)
    assert conn is not None
    assert conn.status == "connected"
    assert conn.provider_id == _TEST_PROVIDER_ID


# ---------------------------------------------------------------------------
# Tests: disconnect_user_connection
# ---------------------------------------------------------------------------


@pytest.mark.service
@pytest.mark.asyncio
async def test_disconnect_returns_false_when_not_connected(
    db_session: AsyncSession, user_and_org: tuple[UUID, UUID]
) -> None:
    user_id, _ = user_and_org
    actor = Actor.user(user_id=user_id)
    removed = await disconnect_user_connection(user_id, _TEST_PROVIDER_ID, actor=actor, session=db_session)
    assert removed is False


@pytest.mark.service
@pytest.mark.asyncio
async def test_disconnect_removes_connection_and_audits(
    db_session: AsyncSession, user_and_org: tuple[UUID, UUID]
) -> None:
    """disconnect_user_connection removes the row and writes an audit row per membership."""
    user_id, org_id = user_and_org

    _STUB.device_response = _device_auth_response()
    await start_device_auth(user_id, _TEST_PROVIDER_ID, session=db_session)
    await db_session.flush()

    _STUB.token_result = _make_tokens("at")
    actor = Actor.user(user_id=user_id)
    await poll_device_auth(user_id, _TEST_PROVIDER_ID, actor=actor, session=db_session)
    await db_session.flush()

    removed = await disconnect_user_connection(user_id, _TEST_PROVIDER_ID, actor=actor, session=db_session)
    await db_session.flush()

    assert removed is True

    conn_row = (
        await db_session.execute(
            select(UserOAuthConnectionRow).where(
                UserOAuthConnectionRow.user_id == user_id,
                UserOAuthConnectionRow.provider_id == _TEST_PROVIDER_ID,
            )
        )
    ).scalar_one_or_none()
    assert conn_row is None

    audit_rows = await list_for_entity(
        "user", user_id, org_id=org_id, kinds=["oauth_connection.disconnected"]
    )
    assert len(audit_rows) == 1
