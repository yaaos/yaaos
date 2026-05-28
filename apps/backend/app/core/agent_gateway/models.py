"""SQLAlchemy models for the agent gateway.

`workspace_agents` — per-pod identity row. One row per `(org_id, agent_pod_id)`;
multiple pods sharing an org's IAM role are normal (ECS service scaled to N tasks).
The same logical agent role is identified across pods by `iam_arn`; per-pod
liveness is tracked separately.

`bearer_tokens` — ledger of issued bearers. One row per `/identity/exchange`
success. `token_hash` is sha256 of the plaintext; plaintext is returned to the
caller exactly once and is never persisted or logged. Revocation flips
`revoked_at` to non-null. Authentication on every other gateway call hashes
the incoming bearer and looks it up here.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, LargeBinary, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WorkspaceAgentRow(Base):
    """One row per agent pod that has exchanged identity. The control
    plane picks pods to dispatch to by joining on `(org_id, state=reachable)`
    + ordering by `last_heartbeat_at desc`."""

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


class BearerTokenRow(Base):
    """Issued bearer tokens. `token_hash` is sha256 of the plaintext —
    plaintext is returned at issuance and never persisted. Authentication
    on every gateway call hashes the incoming bearer and looks it up here.

    `revoked_reason` is one of: `arn_change`, `mode_switch`, `disconnect`,
    `manual_rotate`, `agent_loss`. See plan/notes/finish_m05.md bearer-revoke
    policy table for when each is used.
    """

    __tablename__ = "bearer_tokens"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("workspace_agents.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_ip: Mapped[str | None] = mapped_column(INET, nullable=True)

    __table_args__ = (Index("ix_bearer_tokens_org_issued", "org_id", "issued_at"),)
