"""SQLAlchemy model for `workspace_agents`.

Per-pod identity row. One row per `(org_id, agent_pod_id)`; multiple
pods sharing an org's IAM role are normal (ECS service scaled to
N tasks). The same logical agent role is identified across pods by
`iam_arn`; per-pod liveness is tracked separately.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WorkspaceAgentRow(Base):
    """One row per agent pod that has exchanged identity. The control
    plane picks pods to dispatch to by joining on `(org_id, state=reachable)`
    + ordering by `last_heartbeat_at desc` (least-loaded provisioning
    policy lands in a follow-on iteration)."""

    __tablename__ = "workspace_agents"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    agent_pod_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    iam_arn: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str | None] = mapped_column(String, nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state: Mapped[str] = mapped_column(String, nullable=False, default="reachable")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("org_id", "agent_pod_id", name="uq_workspace_agents_org_pod"),
        Index("ix_workspace_agents_org_heartbeat", "org_id", "last_heartbeat_at"),
    )
