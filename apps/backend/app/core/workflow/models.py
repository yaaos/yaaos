"""SQLAlchemy models owned by `core/workflow`."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WorkflowExecutionRow(Base):
    """One row per in-flight workflow run. State machine:
    `pending → running → (awaiting_agent | awaiting_human)* → done | failed | cancelled`.
    See `apps/backend/docs/core_workflow.md` + the architecture doc."""

    __tablename__ = "workflow_executions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()"))
    ticket_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    workflow_name: Mapped[str] = mapped_column(String, nullable=False)
    workflow_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    current_step_id: Mapped[str | None] = mapped_column(String, nullable=True)
    pending_agent_command_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    step_state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    cancel_requested: Mapped[bool] = mapped_column(nullable=False, default=False)
    otel_trace_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_workflow_executions_state", "state"),
        Index("ix_workflow_executions_pending_agent_command_id", "pending_agent_command_id"),
        Index("ix_workflow_executions_ticket_id", "ticket_id"),
    )


class PendingHumanDecisionRow(Base):
    """One row per HITL pause. Workflow engine writes the question on pause;
    UI handler writes the resolution + transitions the workflow back to
    `running` in the same transaction it enqueues the next step."""

    __tablename__ = "pending_human_decisions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()"))
    workflow_execution_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    question_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    resolution_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index(
            "ix_pending_human_decisions_workflow_resolved",
            "workflow_execution_id",
            "resolved_at",
        ),
    )
