"""SQLAlchemy models for coding-agent run lifecycle.

- `CodingAgentRunRow` (`coding_agent_runs`) — one row per `InvokeClaudeCode`
  execution. Created at dispatch (status=running) and finalized at terminal
  event (status=success|failure). Token usage is NULL; duration is derived
  by `finalize_run` from (`started_at` → terminal-event time).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CodingAgentRunRow(Base):
    """One row per `InvokeClaudeCode` execution.

    Identity = `id` UUID (v7, server-minted). Covers every remote
    coding-agent command kind; today only `review` runs.

    `status` transitions: `running` (created at dispatch) →
    `success` / `failure` (written by the run-sink on terminal event).
    """

    __tablename__ = "coding_agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    # Tenant scope — soft FK (no DB FK), consistent with workspaces.org_id.
    org_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    # Soft FK → workflow_executions(id); cross-module, no DB FK.
    workflow_execution_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    # The workflow step that dispatched this run.
    step_id: Mapped[str] = mapped_column(String, nullable=False)
    # Soft FK → agent_commands(id); 1:1 with the command.
    agent_command_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    # Reporting dimension — e.g. "review".
    command_kind: Mapped[str] = mapped_column(String, nullable=False)
    # Requested model/effort; nullable — not all invocations carry these.
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    effort: Mapped[str | None] = mapped_column(String, nullable=True)
    # running | success | failure — code-enforced, not a DB enum.
    status: Mapped[str] = mapped_column(String, nullable=False)
    # Token usage — NULL today; finalize_run writes the columns when
    # usage-parsing is added to the run-sink.
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Duration — set by finalize_run from (started_at → terminal event time).
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Exit code from the agent subprocess — set by finalize_run.
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Timing — started_at is dispatch time; completed_at is terminal-event time.
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Reporting query: GROUP BY command_kind over (org_id, created_at window).
        Index("ix_coding_agent_runs_org_kind_created", "org_id", "command_kind", "created_at"),
    )
