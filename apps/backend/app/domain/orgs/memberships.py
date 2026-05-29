"""Public membership reads for `domain/orgs`."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy.models import MembershipRow


async def list_active_member_ids(org_id: UUID, *, session: AsyncSession) -> list[UUID]:
    """Return the user_id of every current member of `org_id`.

    "Active" here means has a membership row — removal deletes the row, so all
    rows in `memberships` are by definition active.
    """
    rows = (
        (await session.execute(select(MembershipRow.user_id).where(MembershipRow.org_id == org_id)))
        .scalars()
        .all()
    )
    return list(rows)
