"""Public membership reads for `domain/orgs`."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import list_active_member_ids as _tenancy_list_active_member_ids


async def list_active_member_ids(org_id: UUID, *, session: AsyncSession) -> list[UUID]:
    """Return the user_id of every current member of `org_id`.

    "Active" here means has a membership row — removal deletes the row, so all
    rows in `memberships` are by definition active. Delegates to core/tenancy.
    """
    return await _tenancy_list_active_member_ids(session, org_id)
