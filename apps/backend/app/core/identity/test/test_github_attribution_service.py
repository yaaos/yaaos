"""Service tests for `resolve_github_attribution` — github username → single
active org member, with a `ticket.attribution_failed` audit row on failure.
"""

from __future__ import annotations

import uuid
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import list_for_entity
from app.core.auth import Role
from app.core.identity import create_user, resolve_github_attribution, update_github_handle
from app.domain.orgs import create_membership, create_org

pytestmark = pytest.mark.service


@pytest_asyncio.fixture
async def org_id(db_session: AsyncSession) -> UUID:
    org = await create_org(db_session, slug="attr-test-org", display_name="Attr Org")
    await db_session.flush()
    return org.id


async def _member(
    db_session: AsyncSession, org_id: UUID, *, handle: str, github_username: str | None
) -> UUID:
    user = await create_user(db_session, display_name=f"User {handle}")
    if github_username is not None:
        await update_github_handle(db_session, user_id=user.id, github_username=github_username)
    await create_membership(db_session, org_id=org_id, user_id=user.id, role=Role.BUILDER, handle=handle)
    await db_session.flush()
    return user.id


async def _failure_audits(ticket_id: UUID, org_id: UUID) -> list:
    return await list_for_entity("ticket", ticket_id, org_id=org_id, kinds=["ticket.attribution_failed"])


@pytest.mark.asyncio
async def test_resolves_single_active_member(db_session: AsyncSession, org_id: UUID) -> None:
    user_id = await _member(db_session, org_id, handle="octo", github_username="octocat")
    ticket_id = uuid.uuid7()

    resolved = await resolve_github_attribution(
        "octocat", org_id=org_id, ticket_id=ticket_id, session=db_session
    )

    assert resolved == user_id
    assert await _failure_audits(ticket_id, org_id) == []


@pytest.mark.asyncio
async def test_blank_username_fails_with_audit(db_session: AsyncSession, org_id: UUID) -> None:
    ticket_id = uuid.uuid7()

    resolved = await resolve_github_attribution("", org_id=org_id, ticket_id=ticket_id, session=db_session)

    assert resolved is None
    entries = await _failure_audits(ticket_id, org_id)
    assert len(entries) == 1
    assert entries[0].payload["reason"] == "blank_username"


@pytest.mark.asyncio
async def test_unknown_username_fails_with_audit(db_session: AsyncSession, org_id: UUID) -> None:
    ticket_id = uuid.uuid7()

    resolved = await resolve_github_attribution(
        "ghost", org_id=org_id, ticket_id=ticket_id, session=db_session
    )

    assert resolved is None
    entries = await _failure_audits(ticket_id, org_id)
    assert len(entries) == 1
    assert entries[0].payload["github_username"] == "ghost"
    assert entries[0].payload["reason"] == "no_matching_user"


@pytest.mark.asyncio
async def test_collision_fails_with_audit(db_session: AsyncSession, org_id: UUID) -> None:
    await _member(db_session, org_id, handle="one", github_username="shared")
    await _member(db_session, org_id, handle="two", github_username="shared")
    ticket_id = uuid.uuid7()

    resolved = await resolve_github_attribution(
        "shared", org_id=org_id, ticket_id=ticket_id, session=db_session
    )

    assert resolved is None
    entries = await _failure_audits(ticket_id, org_id)
    assert len(entries) == 1
    assert entries[0].payload["reason"] == "username_collision"


@pytest.mark.asyncio
async def test_non_member_fails_with_audit(db_session: AsyncSession, org_id: UUID) -> None:
    user = await create_user(db_session, display_name="Outsider")
    await update_github_handle(db_session, user_id=user.id, github_username="outsider")
    await db_session.flush()
    ticket_id = uuid.uuid7()

    resolved = await resolve_github_attribution(
        "outsider", org_id=org_id, ticket_id=ticket_id, session=db_session
    )

    assert resolved is None
    entries = await _failure_audits(ticket_id, org_id)
    assert len(entries) == 1
    assert entries[0].payload["reason"] == "not_an_active_member"
