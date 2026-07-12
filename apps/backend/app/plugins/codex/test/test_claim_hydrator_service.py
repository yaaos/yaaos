"""Service tests for the codex claim-time credential hydrator.

Covers _codex_command_hydrator's per_user and api_key branches:
- api_key mode (credential_user_id=None): strips _org_id, no auth_json added.
- per_user mode, connected fresh token: output carries auth_json (SecretStr),
  credential_user_id preserved (Go agent uses it as the signal to write auth.json).
- per_user mode, no connection: raises CredentialHydrationError.
- per_user mode, needs_reauth: raises CredentialHydrationError.

Direct-invocation style (no claim_next machinery) — the hydrator is tested as a
pure transformation over a payload dict; DB state drives `ensure_fresh_access_token`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_gateway import HydrationContext
from app.core.secrets import encrypt
from app.plugins.codex.service import _codex_command_hydrator

pytestmark = [pytest.mark.service]

# ── Helpers ───────────────────────────────────────────────────────────────────


_TEST_ORG_ID = uuid4()


def _make_payload(*, credential_user_id: UUID | None, wallclock_seconds: int = 900) -> dict:
    """Minimal InvokeCodex command payload for hydrator input."""
    return {
        "kind": "InvokeCodex",
        "command_id": str(uuid4()),
        "workspace_id": str(uuid4()),
        "credential_user_id": str(credential_user_id) if credential_user_id else None,
        "limits": {"wallclock_seconds": wallclock_seconds},
        "skill_path": ".codex/skills/test/SKILL.md",
    }


def _make_ctx(org_id: UUID | None = None) -> HydrationContext:
    return HydrationContext(org_id=org_id or _TEST_ORG_ID)


async def _seed_connection_row(
    db_session: AsyncSession,
    *,
    user_id: UUID,
    status: str = "connected",
    expires_at: datetime | None = None,
) -> None:
    """Insert a user_oauth_connections row via raw SQL for test seeding.

    test/ dirs are exempt from bin/check_table_access's raw-SQL ownership scan.
    Also inserts a users row for the given user_id (FK requirement).
    """
    import app.plugins.codex  # noqa: PLC0415 — ensures UserOAuthApp registration

    _ = app.plugins.codex

    # Ensure the users FK target exists.
    await db_session.execute(
        text(
            "INSERT INTO users (id, display_name) VALUES (:uid, 'hydrator-test') ON CONFLICT (id) DO NOTHING"
        ),
        {"uid": user_id},
    )
    await db_session.flush()

    if expires_at is None:
        expires_at = datetime.now(UTC) + timedelta(hours=8)

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


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_api_key_mode_strips_org_id_no_auth_json(db_session: AsyncSession) -> None:
    """api_key mode (credential_user_id=None): org_id not in output, auth_json absent."""
    payload = _make_payload(credential_user_id=None)
    ctx = _make_ctx()

    result = await _codex_command_hydrator(payload, ctx, db_session)

    assert "_org_id" not in result, "_org_id must not appear in the output"
    assert "auth_json" not in result or result.get("auth_json") is None, (
        "api_key mode must not inject auth_json"
    )
    # credential_user_id should be None/absent (it was None in input)
    assert result.get("credential_user_id") is None


@pytest.mark.asyncio
async def test_per_user_connected_injects_auth_json(db_session: AsyncSession) -> None:
    """per_user mode with fresh token: output has auth_json (SecretStr) and
    credential_user_id preserved. _org_id is stripped."""
    user_id = uuid4()
    await _seed_connection_row(db_session, user_id=user_id, status="connected")

    payload = _make_payload(credential_user_id=user_id)
    ctx = _make_ctx()

    result = await _codex_command_hydrator(payload, ctx, db_session)

    assert "_org_id" not in result, "_org_id must not appear in the output"
    assert "auth_json" in result, "per_user mode must inject auth_json"
    auth_json = result["auth_json"]
    assert isinstance(auth_json, SecretStr), "auth_json must be a SecretStr (not a plaintext str)"
    # Verify the JSON content has the right shape
    import json  # noqa: PLC0415

    content = json.loads(auth_json.get_secret_value())
    assert content.get("auth_mode") == "chatgptAuthTokens"
    assert "tokens" in content
    assert content["tokens"].get("access_token") == "test-access-token"
    # credential_user_id preserved so Go agent knows to write auth.json
    assert result.get("credential_user_id") == str(user_id)


@pytest.mark.asyncio
async def test_per_user_no_connection_raises_hydration_error(db_session: AsyncSession) -> None:
    """per_user mode with no connection row → CredentialHydrationError."""
    import app.plugins.codex  # noqa: PLC0415

    _ = app.plugins.codex

    from app.core.agent_gateway import CredentialHydrationError  # noqa: PLC0415

    user_id = uuid4()  # no row seeded
    payload = _make_payload(credential_user_id=user_id)
    ctx = _make_ctx()

    with pytest.raises(CredentialHydrationError, match="not connected"):
        await _codex_command_hydrator(payload, ctx, db_session)


@pytest.mark.asyncio
async def test_per_user_needs_reauth_raises_hydration_error(db_session: AsyncSession) -> None:
    """per_user mode with needs_reauth connection → CredentialHydrationError."""
    from app.core.agent_gateway import CredentialHydrationError  # noqa: PLC0415

    user_id = uuid4()
    await _seed_connection_row(db_session, user_id=user_id, status="needs_reauth")

    payload = _make_payload(credential_user_id=user_id)
    ctx = _make_ctx()

    with pytest.raises(CredentialHydrationError, match="re-authorization"):
        await _codex_command_hydrator(payload, ctx, db_session)
