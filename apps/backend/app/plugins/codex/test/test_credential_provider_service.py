"""Service tests for the codex dispatch-time credential provider.

Covers `_codex_credential_provider` — the only credential mode Codex
supports is api_key (org-level OpenAI API key via `core/api_keys`).

No unittest.mock.patch — behaviour is driven by DB state and injected callables.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor
from app.core.coding_agent import install_coding_agent
from app.core.tenancy import create_org
from app.plugins.codex.service import _codex_credential_provider

pytestmark = [pytest.mark.service]

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def org_id(db_session: AsyncSession) -> UUID:
    """Seed a fresh org."""
    org = await create_org(db_session, slug=f"codex-cred-{uuid4().hex[:8]}", display_name="Codex Org")
    return org.org_id


async def _install_codex(db_session: AsyncSession, org_id: UUID) -> None:
    """Install the codex plugin for org with empty settings."""
    import app.plugins.codex  # noqa: PLC0415 — triggers bootstrap

    _ = app.plugins.codex  # ensure import side-effect runs
    await install_coding_agent(
        db_session,
        org_id=org_id,
        plugin_id="codex",
        settings={},
        actor=Actor.system(),
    )
    await db_session.flush()


# ── api_key mode ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_api_key_mode_no_key_raises(db_session: AsyncSession, org_id: UUID) -> None:
    """No openai key configured → CredentialUnavailableError."""
    from app.core.coding_agent import CredentialUnavailableError  # noqa: PLC0415

    await _install_codex(db_session, org_id)

    with pytest.raises(CredentialUnavailableError, match="No OpenAI API key"):
        await _codex_credential_provider(
            org_id=org_id,
            user_id=None,
            wallclock_seconds=900,
            session=db_session,
        )


@pytest.mark.asyncio
async def test_api_key_mode_with_key_returns_spec(db_session: AsyncSession, org_id: UUID) -> None:
    """openai key configured → CommandCredentialSpec(credential_user_id=None)."""
    import app.core.api_keys as api_keys  # noqa: PLC0415
    from app.core.coding_agent import CommandCredentialSpec  # noqa: PLC0415

    await _install_codex(db_session, org_id)
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
