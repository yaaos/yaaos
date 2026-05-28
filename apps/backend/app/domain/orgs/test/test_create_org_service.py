"""Service tests for ``orgs.create_org`` and ``orgs.create_membership``."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.audit_log import Actor, AuditEntryRow
from app.domain.identity import create_user
from app.domain.orgs import MembershipRow, OrgRow, create_membership, create_org
from app.domain.orgs.types import Role

# ---------------------------------------------------------------------------
# create_org
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.service
async def test_create_org_persists_row(db_session) -> None:
    """Happy path: ``create_org`` inserts an ``orgs`` row with the given slug."""
    org = await create_org(db_session, slug="test-org-1", display_name="Test Org 1")
    await db_session.commit()

    row = (await db_session.execute(select(OrgRow).where(OrgRow.id == org.id))).scalar_one_or_none()

    assert row is not None
    assert row.slug == "test-org-1"
    assert row.display_name == "Test Org 1"


@pytest.mark.asyncio
@pytest.mark.service
async def test_create_org_returns_org_value_object(db_session) -> None:
    """``create_org`` returns an ``Org`` value object matching the inserted row."""
    org = await create_org(db_session, slug="test-org-2", display_name="Test Org 2")

    assert org.slug == "test-org-2"
    assert org.display_name == "Test Org 2"
    assert org.id is not None


@pytest.mark.asyncio
@pytest.mark.service
async def test_create_org_emits_audit_row(db_session) -> None:
    """``create_org`` emits an ``org.created`` audit row."""
    org = await create_org(db_session, slug="test-org-audit", display_name="Audit Org")
    await db_session.commit()

    audit_rows = (
        (
            await db_session.execute(
                select(AuditEntryRow).where(
                    AuditEntryRow.entity_id == org.id,
                    AuditEntryRow.kind == "org.created",
                )
            )
        )
        .scalars()
        .all()
    )

    assert len(audit_rows) == 1
    assert audit_rows[0].entity_kind == "org"
    assert audit_rows[0].payload["slug"] == "test-org-audit"


# ---------------------------------------------------------------------------
# create_membership
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.service
async def test_create_membership_persists_row(db_session) -> None:
    """Happy path: ``create_membership`` inserts a ``memberships`` row."""
    org = await create_org(db_session, slug="test-mem-org-1", display_name="Mem Org 1")
    user = await create_user(db_session, display_name="Test Owner")

    membership = await create_membership(
        db_session,
        user_id=user.id,
        org_id=org.id,
        role=Role.OWNER,
        handle="testowner",
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(MembershipRow).where(
                MembershipRow.user_id == user.id,
                MembershipRow.org_id == org.id,
            )
        )
    ).scalar_one_or_none()

    assert row is not None
    assert row.role == "owner"
    assert membership.role == Role.OWNER


@pytest.mark.asyncio
@pytest.mark.service
async def test_create_membership_emits_audit_row(db_session) -> None:
    """``create_membership`` emits a ``membership.created`` audit row."""
    org = await create_org(db_session, slug="test-mem-audit-org", display_name="Audit Mem Org")
    user = await create_user(db_session, display_name="Audit User")

    await create_membership(
        db_session,
        user_id=user.id,
        org_id=org.id,
        role=Role.BUILDER,
        handle="auditbuilder",
        actor=Actor.system(),
    )
    await db_session.commit()

    audit_rows = (
        (
            await db_session.execute(
                select(AuditEntryRow).where(
                    AuditEntryRow.entity_id == org.id,
                    AuditEntryRow.kind == "membership.created",
                )
            )
        )
        .scalars()
        .all()
    )

    assert len(audit_rows) == 1
    assert audit_rows[0].payload["role"] == "builder"
