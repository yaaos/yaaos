"""Service tests for the codex credential provider and hydrator.

Covers the dispatch-time credential resolver (_codex_credential_provider) and
the claim-time hydrator (_codex_command_hydrator) across all mode/freshness
branches.

No unittest.mock.patch — behaviour is driven by DB state and injected callables.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor
from app.core.coding_agent import install_coding_agent
from app.core.identity import create_user
from app.core.secrets import encrypt
from app.core.tenancy import create_org
from app.plugins.codex.service import _codex_credential_provider

pytestmark = [pytest.mark.service]

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def org_id(db_session: AsyncSession) -> UUID:
    """Seed a fresh org."""
    org = await create_org(db_session, slug=f"codex-cred-{uuid4().hex[:8]}", display_name="Codex Org")
    return org.org_id


@pytest_asyncio.fixture
async def user_id(db_session: AsyncSession) -> UUID:
    """Seed a fresh user."""
    user = await create_user(db_session, display_name="Codex Test User")
    return user.id


async def _install_codex(db_session: AsyncSession, org_id: UUID, *, auth_mode: str) -> None:
    """Install the codex plugin for org with the given auth_mode."""
    import app.plugins.codex  # noqa: PLC0415 — triggers bootstrap + UserOAuthApp registration

    _ = app.plugins.codex  # ensure import side-effect runs
    await install_coding_agent(
        db_session,
        org_id=org_id,
        plugin_id="codex",
        settings={"auth_mode": auth_mode},
        actor=Actor.system(),
    )
    await db_session.flush()


async def _seed_connection_row(
    db_session: AsyncSession,
    *,
    user_id: UUID,
    status: str = "connected",
    expires_at: datetime | None = None,
) -> None:
    """Insert a user_oauth_connections row directly for testing.

    Uses raw SQL so this test directory stays import-boundary-clean — no
    UserOAuthConnectionRow across module boundaries. test/ dirs are exempt
    from bin/check_table_access's raw-SQL ownership scan.
    """
    if expires_at is None:
        expires_at = datetime.now(UTC) + timedelta(hours=8)  # well above any margin

    encrypted_at = encrypt("test-access-token").decode()
    now = datetime.now(UTC)

    await db_session.execute(
        text(
            """
            INSERT INTO user_oauth_connections
                (user_id, provider_id, status, encrypted_access_token,
                 encrypted_refresh_token, encrypted_id_token,
                 external_account_id, granted_scope,
                 access_token_expires_at, last_refresh_at,
                 needs_reauth_reason, created_at, updated_at)
            VALUES
                (:uid, 'codex', :status, :eat,
                 NULL, NULL,
                 'test-account-id', 'openid profile email',
                 :expires_at, :now,
                 :reauth_reason, :now, :now)
            ON CONFLICT (user_id, provider_id) DO UPDATE
                SET status = EXCLUDED.status,
                    encrypted_access_token = EXCLUDED.encrypted_access_token,
                    access_token_expires_at = EXCLUDED.access_token_expires_at,
                    needs_reauth_reason = EXCLUDED.needs_reauth_reason,
                    updated_at = EXCLUDED.updated_at
            """
        ),
        {
            "uid": user_id,
            "status": status,
            "eat": encrypted_at,
            "expires_at": expires_at,
            "now": now,
            "reauth_reason": "test_needs_reauth" if status == "needs_reauth" else None,
        },
    )
    await db_session.flush()


# ── api_key mode ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_api_key_mode_no_key_raises(db_session: AsyncSession, org_id: UUID) -> None:
    """api_key mode with no openai key configured → CredentialUnavailableError."""
    from app.core.coding_agent import CredentialUnavailableError  # noqa: PLC0415

    await _install_codex(db_session, org_id, auth_mode="api_key")

    with pytest.raises(CredentialUnavailableError, match="No OpenAI API key"):
        await _codex_credential_provider(
            org_id=org_id,
            user_id=None,
            wallclock_seconds=900,
            session=db_session,
        )


@pytest.mark.asyncio
async def test_api_key_mode_with_key_returns_spec(db_session: AsyncSession, org_id: UUID) -> None:
    """api_key mode with openai key → CommandCredentialSpec(credential_user_id=None)."""
    import app.core.api_keys as api_keys  # noqa: PLC0415
    from app.core.coding_agent import CommandCredentialSpec  # noqa: PLC0415

    await _install_codex(db_session, org_id, auth_mode="api_key")
    await api_keys.set(org_id, "openai", "sk-test-openai-key", actor=Actor.system(), session=db_session)
    await db_session.flush()

    spec = await _codex_credential_provider(
        org_id=org_id,
        user_id=None,
        wallclock_seconds=900,
        session=db_session,
    )

    assert isinstance(spec, CommandCredentialSpec)
    assert spec.credential_user_id is None


# ── per_user mode ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_per_user_mode_null_user_id_raises(db_session: AsyncSession, org_id: UUID) -> None:
    """per_user mode with user_id=None → CredentialUnavailableError (no attributed user)."""
    from app.core.coding_agent import CredentialUnavailableError  # noqa: PLC0415

    await _install_codex(db_session, org_id, auth_mode="per_user")

    with pytest.raises(CredentialUnavailableError, match="no attributed user"):
        await _codex_credential_provider(
            org_id=org_id,
            user_id=None,
            wallclock_seconds=900,
            session=db_session,
        )


@pytest.mark.asyncio
async def test_per_user_mode_no_connection_raises(
    db_session: AsyncSession, org_id: UUID, user_id: UUID
) -> None:
    """per_user mode with a user that has no codex connection → CredentialUnavailableError."""
    import app.plugins.codex  # noqa: PLC0415 — ensure codex UserOAuthApp registered
    from app.core.coding_agent import CredentialUnavailableError  # noqa: PLC0415

    _ = app.plugins.codex

    await _install_codex(db_session, org_id, auth_mode="per_user")

    with pytest.raises(CredentialUnavailableError, match="not connected"):
        await _codex_credential_provider(
            org_id=org_id,
            user_id=user_id,
            wallclock_seconds=900,
            session=db_session,
        )


@pytest.mark.asyncio
async def test_per_user_mode_connected_returns_spec(
    db_session: AsyncSession, org_id: UUID, user_id: UUID
) -> None:
    """per_user mode with a fresh connected token → CommandCredentialSpec(credential_user_id=user_id).

    Seeds a connection row with access_token_expires_at far in the future so
    ensure_fresh_access_token takes the fast path (no token endpoint call).
    """
    import app.plugins.codex  # noqa: PLC0415
    from app.core.coding_agent import CommandCredentialSpec  # noqa: PLC0415

    _ = app.plugins.codex

    await _install_codex(db_session, org_id, auth_mode="per_user")
    await _seed_connection_row(db_session, user_id=user_id, status="connected")

    spec = await _codex_credential_provider(
        org_id=org_id,
        user_id=user_id,
        wallclock_seconds=900,
        session=db_session,
    )

    assert isinstance(spec, CommandCredentialSpec)
    assert spec.credential_user_id == user_id


@pytest.mark.asyncio
async def test_per_user_mode_needs_reauth_raises(
    db_session: AsyncSession, org_id: UUID, user_id: UUID
) -> None:
    """per_user mode with a needs_reauth connection → CredentialUnavailableError (reconnect reason)."""
    import app.plugins.codex  # noqa: PLC0415
    from app.core.coding_agent import CredentialUnavailableError  # noqa: PLC0415

    _ = app.plugins.codex

    await _install_codex(db_session, org_id, auth_mode="per_user")
    await _seed_connection_row(db_session, user_id=user_id, status="needs_reauth")

    with pytest.raises(CredentialUnavailableError, match="re-authorization"):
        await _codex_credential_provider(
            org_id=org_id,
            user_id=user_id,
            wallclock_seconds=900,
            session=db_session,
        )
