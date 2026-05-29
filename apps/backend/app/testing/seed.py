"""Seed helpers for service tests.

Functions that insert canonical test rows (orgs, users, memberships, etc.)
via production service APIs so each service test starts from a known, minimal
state without coupling to DB models.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_gateway import ensure_agent_row

__all__ = ["seed_agent"]


async def seed_agent(
    *,
    org_id: UUID,
    session: AsyncSession,
    iam_arn: str = "arn:aws:iam::123456789012:role/yaaos-agent",
    version: str = "0.0.1",
    heartbeat_age_seconds: int = 0,
) -> dict:
    """Insert a reachable workspace-agent row for testing.

    Returns a dict with `id` (row PK), `agent_pod_id` (pod UUID), and
    `org_id`. Backdates `last_heartbeat_at` when `heartbeat_age_seconds > 0`.
    """
    from app.core.agent_gateway.models import WorkspaceAgentRow  # noqa: PLC0415

    pod_id = uuid4()
    agent_id = await ensure_agent_row(
        org_id=org_id,
        agent_pod_id=pod_id,
        iam_arn=iam_arn,
        version=version,
        session=session,
    )
    if heartbeat_age_seconds > 0:
        row = await session.get(WorkspaceAgentRow, agent_id)
        if row is not None:
            row.last_heartbeat_at = datetime.now(UTC) - timedelta(seconds=heartbeat_age_seconds)
            await session.flush()
    return {"id": agent_id, "agent_pod_id": pod_id, "org_id": org_id}
