"""SQLAlchemy models for the agent gateway.

`workspace_agents` — per-pod identity row. One row per `(org_id, instance_id)`;
multiple pods sharing an org's IAM role are normal (ECS service scaled to N tasks).
`instance_id` is the role-session-name from the STS assumed-role ARN — derived
by the backend on identity exchange, never supplied by the agent. The same
`instance_id` is stable across pod restarts as long as the ECS task uses the same
session name.

`bearer_tokens` — ledger of issued bearers. One row per `/api/v1/agent/identity`
success. `token_hash` is sha256 of the plaintext; plaintext is returned to the
caller exactly once and is never persisted or logged. Revocation flips
`revoked_at` to non-null. Authentication on every other gateway call hashes
the incoming bearer and looks it up here.

`agent_commands` — durable command queue. Enqueued atomically with the workspace
single-flight claim; the agent claims a capacity-bounded batch via lease
(pending→claimed→delivered→done). Backend restarts lose no commands.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WorkspaceAgentRow(Base):
    """One row per agent pod that has exchanged identity. The control
    plane picks pods to dispatch to by joining on `(org_id, state=reachable)`
    + ordering by `last_heartbeat_at desc`."""

    __tablename__ = "workspace_agents"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    # instance_id is the role-session-name segment of the STS assumed-role ARN.
    # Derived by the backend on exchange; never provided by the agent.
    # Stable across pod restarts when the ECS task reuses the same session name.
    instance_id: Mapped[str] = mapped_column(String, nullable=False)
    iam_arn: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str | None] = mapped_column(String, nullable=True)
    # Static OS metadata reported once at identity exchange.
    os: Mapped[str | None] = mapped_column(String, nullable=True)
    cpu_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    memory_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_shutdown_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # claimed_workspace_count: populated by the heartbeat path (not identity exchange).
    claimed_workspace_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String, nullable=False, default="reachable")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("org_id", "instance_id", name="uq_workspace_agents_org_instance"),
        Index("ix_workspace_agents_instance_id", "instance_id", unique=True),
        Index("ix_workspace_agents_org_heartbeat", "org_id", "last_heartbeat_at"),
    )


class BearerTokenRow(Base):
    """Issued bearer tokens. `token_hash` is sha256 of the plaintext —
    plaintext is returned at issuance and never persisted. Authentication
    on every gateway call hashes the incoming bearer and looks it up here.

    `revoked_reason` is one of: `arn_change`, `mode_switch`, `disconnect`,
    `manual_rotate`, `agent_loss`.
    """

    __tablename__ = "bearer_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
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
    # IAM ARN of the agent pod that was verified at issuance. Canonical form
    # (iam::ACCT:role/ROLE, lowercased). Recorded for audit.
    issued_iam_arn: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("ix_bearer_tokens_org_issued", "org_id", "issued_at"),
        Index("ix_bearer_tokens_issued_iam_arn", "issued_iam_arn"),
    )


class AgentCommandRow(Base):
    """Durable command queue. One row per dispatched AgentCommand.

    Commands flow: pending → claimed → delivered → done.
    The lease window is 30 s: if no `received` event arrives within 30 s of
    `claimed_at`, the reaper flips `claimed` back to `pending` for re-delivery.
    `attempt` increments on each requeue; when it hits the cap the command is
    marked done with a terminal-failure outcome.

    `org_id` and `workspace_id` are informational indexes — `workspace_id` is
    NULL for org-scoped commands (e.g. ConfigUpdate, CreateWorkspace before an
    agent is assigned). `agent_id` is stamped at claim time, not enqueue time.
    """

    __tablename__ = "agent_commands"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    # workspace_id is NULL for org-scoped commands (ConfigUpdate, CreateWorkspace).
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    # command_kind discriminates CreateWorkspace (org-scoped) from workspace-pinned commands.
    command_kind: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # status lifecycle: pending → claimed → delivered → done
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    # agent_id is stamped at claim time. NULL until claimed.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # attempt counts re-queues; capped to prevent infinite retry.
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Fast lookup for the agent's capacity-pull claim (FIFO via UUIDv7 id).
        Index("ix_agent_commands_agent_status_id", "agent_id", "status", "id"),
        # Fast lookup for unassigned CreateWorkspace commands (org-scoped claim).
        Index("ix_agent_commands_status_kind_id", "status", "command_kind", "id"),
    )
