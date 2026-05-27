"""Service-layer tests for `domain/orgs` — `get_org`, `delete_expired_invitations`, `find_saml_org_slug_for_domain`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.domain.orgs import delete_expired_invitations, find_saml_org_slug_for_domain, get_org
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs.types import Role

# ---------------------------------------------------------------------------
# get_org
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_org_returns_org(db_session) -> None:
    """Happy path: returns an Org value object with the right attributes."""
    row = await orgs_repo.insert_org(db_session, slug="get-org-happy", display_name="Happy Org")
    await db_session.commit()

    org = await get_org(row.id)

    assert org is not None
    assert org.id == row.id
    assert org.slug == "get-org-happy"
    assert org.display_name == "Happy Org"


@pytest.mark.asyncio
async def test_get_org_not_found_returns_none(db_session) -> None:
    """Unknown UUID returns None; no exception raised."""
    result = await get_org(uuid4())
    assert result is None


# ---------------------------------------------------------------------------
# delete_expired_invitations
# ---------------------------------------------------------------------------


async def _make_invitation(
    db,
    *,
    org_id,
    expires_at: datetime,
    accepted_at: datetime | None = None,
) -> None:
    """Insert a minimal invitation row directly via the repository."""
    await orgs_repo.insert_invitation(
        db,
        org_id=org_id,
        email=f"user-{uuid4().hex[:8]}@example.com",
        role=Role.BUILDER,
        token_hash=uuid4().hex,
        expires_at=expires_at,
        invited_by_user_id=None,
    )
    if accepted_at is not None:
        # stamp accepted_at via a direct attribute tweak + flush
        from sqlalchemy import select as _select  # noqa: PLC0415

        from app.domain.orgs.models import InvitationRow  # noqa: PLC0415

        inv = (
            await db.execute(_select(InvitationRow).order_by(InvitationRow.created_at.desc()).limit(1))
        ).scalar_one()
        inv.accepted_at = accepted_at
        await db.flush()


@pytest.mark.asyncio
async def test_delete_expired_invitations_counts_only_unaccepted_past_due(db_session) -> None:
    """Only unaccepted+expired rows are deleted; accepted and future rows survive."""
    org = await orgs_repo.insert_org(db_session, slug="del-inv-org")

    now = datetime.now(UTC)

    # Should be deleted — expired, unaccepted
    await _make_invitation(db_session, org_id=org.id, expires_at=now - timedelta(hours=1))
    await _make_invitation(db_session, org_id=org.id, expires_at=now - timedelta(days=7))

    # Should survive — future expiry, unaccepted
    await _make_invitation(db_session, org_id=org.id, expires_at=now + timedelta(days=7))

    # Should survive — expired but already accepted
    await _make_invitation(
        db_session,
        org_id=org.id,
        expires_at=now - timedelta(hours=2),
        accepted_at=now - timedelta(hours=3),
    )

    await db_session.commit()

    count = await delete_expired_invitations()

    assert count == 2


@pytest.mark.asyncio
async def test_delete_expired_invitations_zero_when_none_expired(db_session) -> None:
    """Returns 0 when no invitations are eligible."""
    org = await orgs_repo.insert_org(db_session, slug="del-inv-none")
    await _make_invitation(
        db_session,
        org_id=org.id,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    await db_session.commit()

    count = await delete_expired_invitations()
    assert count == 0


# ---------------------------------------------------------------------------
# find_saml_org_slug_for_domain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_saml_org_slug_for_domain_returns_slug(db_session) -> None:
    """Happy path: enabled config whose domain list contains the queried domain."""
    from app.domain.orgs.sso import upsert_config  # noqa: PLC0415

    org = await orgs_repo.insert_org(db_session, slug="saml-domain-org")
    await upsert_config(
        db_session,
        org_id=org.id,
        idp_metadata_xml="<md/>",
        enabled=True,
        email_domains=["example.com"],
    )
    await db_session.commit()

    slug = await find_saml_org_slug_for_domain("example.com")
    assert slug == "saml-domain-org"


@pytest.mark.asyncio
async def test_find_saml_org_slug_for_domain_returns_none_when_disabled(db_session) -> None:
    """Disabled config must not match."""
    from app.domain.orgs.sso import upsert_config  # noqa: PLC0415

    org = await orgs_repo.insert_org(db_session, slug="saml-disabled-org")
    await upsert_config(
        db_session,
        org_id=org.id,
        idp_metadata_xml="<md/>",
        enabled=False,
        email_domains=["disabled.example.com"],
    )
    await db_session.commit()

    result = await find_saml_org_slug_for_domain("disabled.example.com")
    assert result is None


@pytest.mark.asyncio
async def test_find_saml_org_slug_for_domain_returns_none_when_no_match(db_session) -> None:
    """Unknown domain returns None."""
    result = await find_saml_org_slug_for_domain(f"unknown-{uuid4().hex}.example.com")
    assert result is None
