"""Service tests for the codex `UserOAuthApp.relevance_fn` — the codex
connection card shows only for users in an org running codex in per_user mode.
"""

from __future__ import annotations

from uuid import UUID, uuid7

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor
from app.core.auth import Role
from app.core.coding_agent import install_coding_agent, update_coding_agent_settings
from app.core.identity import create_user
from app.core.oauth import get_user_oauth_app
from app.domain.orgs import create_membership, create_org

pytestmark = pytest.mark.service


@pytest_asyncio.fixture
async def member_in_org(db_session: AsyncSession) -> tuple[UUID, UUID]:
    org = await create_org(db_session, slug=f"rel-{uuid7().hex[:8]}", display_name="Relevance Org")
    user = await create_user(db_session, display_name="Relevance User")
    await create_membership(db_session, org_id=org.id, user_id=user.id, role=Role.BUILDER, handle="reluser")
    await db_session.flush()
    return user.id, org.id


def _relevance_fn():
    fn = get_user_oauth_app("codex").relevance_fn
    assert fn is not None
    return fn


@pytest.mark.asyncio
async def test_no_codex_install_is_irrelevant(
    db_session: AsyncSession, member_in_org: tuple[UUID, UUID]
) -> None:
    user_id, _ = member_in_org
    assert await _relevance_fn()(user_id, db_session) is False


@pytest.mark.asyncio
async def test_api_key_mode_is_irrelevant(db_session: AsyncSession, member_in_org: tuple[UUID, UUID]) -> None:
    user_id, org_id = member_in_org
    await install_coding_agent(
        db_session,
        org_id=org_id,
        plugin_id="codex",
        settings={"auth_mode": "api_key"},
        actor=Actor.user(user_id=user_id),
        created_by=user_id,
    )
    assert await _relevance_fn()(user_id, db_session) is False


@pytest.mark.asyncio
async def test_per_user_mode_is_relevant(db_session: AsyncSession, member_in_org: tuple[UUID, UUID]) -> None:
    user_id, org_id = member_in_org
    await install_coding_agent(
        db_session,
        org_id=org_id,
        plugin_id="codex",
        settings={"auth_mode": "per_user"},
        actor=Actor.user(user_id=user_id),
        created_by=user_id,
    )
    assert await _relevance_fn()(user_id, db_session) is True


@pytest.mark.asyncio
async def test_switching_back_to_api_key_becomes_irrelevant(
    db_session: AsyncSession, member_in_org: tuple[UUID, UUID]
) -> None:
    user_id, org_id = member_in_org
    await install_coding_agent(
        db_session,
        org_id=org_id,
        plugin_id="codex",
        settings={"auth_mode": "per_user"},
        actor=Actor.user(user_id=user_id),
        created_by=user_id,
    )
    await update_coding_agent_settings(
        db_session,
        org_id=org_id,
        plugin_id="codex",
        settings={"auth_mode": "api_key"},
        actor=Actor.user(user_id=user_id),
    )
    assert await _relevance_fn()(user_id, db_session) is False
