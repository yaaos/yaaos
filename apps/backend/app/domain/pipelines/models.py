"""SQLAlchemy rows owned by `domain/pipelines`.

Value objects (the definition model, run/stage read models) live in
`types.py`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PipelineRow(Base):
    """One org-scoped user-defined pipeline definition."""

    __tablename__ = "pipelines"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    org_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    stages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_pipelines_org_name"),)


class PipelineRunRow(Base):
    """One pipeline run."""

    __tablename__ = "pipeline_runs"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()"))
    org_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False)
    ticket_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tickets.id"), nullable=False)
    # Soft ref — no DB constraint. The definition may be deleted later;
    # architecture explicitly calls this a soft ref (unlike the other FKs
    # on this row), mirroring tickets.current_run_id.
    pipeline_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    pipeline_name: Mapped[str] = mapped_column(String, nullable=False)
    definition_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    phase: Mapped[str] = mapped_column(String, nullable=False, server_default="provision")
    current_stage_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    workspace_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    pending_agent_command_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    kickoff: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    sendback_counts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    otel_trace_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('queued','running','paused','completed','failed','killed','cancelled')",
            name="ck_pipeline_runs_state",
        ),
        CheckConstraint("phase IN ('provision','stages','cleanup')", name="ck_pipeline_runs_phase"),
        Index("ix_pipeline_runs_ticket", "ticket_id"),
        Index("ix_pipeline_runs_state", "state"),
        Index(
            "ux_pipeline_runs_one_in_flight",
            "ticket_id",
            unique=True,
            postgresql_where=text("state IN ('running','paused')"),
        ),
    )


class StageExecutionRow(Base):
    """One execution attempt of one stage (incl. engine bookkeeping)."""

    __tablename__ = "stage_executions"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()"))
    org_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False
    )
    stage_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    stage_name: Mapped[str] = mapped_column(String, nullable=False)
    skill_name: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    phase: Mapped[str | None] = mapped_column(String, nullable=True)
    iteration: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    confidence: Mapped[str | None] = mapped_column(String, nullable=True)
    loop_state: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'")
    )
    boundary_outcome: Mapped[str | None] = mapped_column(String, nullable=True)
    boundary_detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    action_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    revision: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("kind IN ('skill','review','action','system')", name="ck_stage_executions_kind"),
        CheckConstraint("status IN ('running','completed','failed')", name="ck_stage_executions_status"),
        CheckConstraint(
            "phase IS NULL OR phase IN ('main','review','fix')", name="ck_stage_executions_phase"
        ),
        CheckConstraint(
            "confidence IS NULL OR confidence IN ('low','medium','high')",
            name="ck_stage_executions_confidence",
        ),
        CheckConstraint(
            "boundary_outcome IS NULL OR boundary_outcome IN ('proceeded','paused','sent_back')",
            name="ck_stage_executions_boundary_outcome",
        ),
        Index("ix_stage_executions_run", "run_id", "stage_index"),
    )


class RunPauseRow(Base):
    """One HITL pause — replaces `pending_human_decisions`."""

    __tablename__ = "run_pauses"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()"))
    org_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False
    )
    stage_execution_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("stage_executions.id"), nullable=False
    )
    tripped: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    escalation_user_ids: Mapped[list[UUID]] = mapped_column(ARRAY(PgUUID(as_uuid=True)), nullable=False)
    resolution: Mapped[str | None] = mapped_column(String, nullable=True)
    instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    send_back_to_stage: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_by: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "resolution IS NULL OR resolution IN ('approve','instruct','send_back','kill')",
            name="ck_run_pauses_resolution",
        ),
        Index("ix_run_pauses_run_open", "run_id", postgresql_where=text("resolved_at IS NULL")),
    )
