"""SQLAlchemy models for `outbox_entries` and `scheduled_runs`.

`outbox_entries` is the pending-dispatch table (folded in from the former
`core/outbox` module; the row shape is generic so new kinds plug in via
dispatchers without schema changes — only `taskiq_enqueue` today).

`scheduled_runs` is the per-tick dedup ledger for the recurring-task
scheduler. The composite PK `(schedule_id, fire_time)` IS the
exactly-once gate: every worker runs the scheduler tick, but the
`INSERT … ON CONFLICT DO NOTHING` decides which one enqueues the slot.

Both rows are private details of `core/tasks`; callers see only the
public `enqueue()` / `@scheduled` / `schedule_task` API.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class OutboxEntryRow(Base):
    __tablename__ = "outbox_entries"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()"))
    kind: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # `created_at` is part of the composite primary key (id, created_at) — see
    # migration 042. The composite PK enables future time-range partitioning
    # by created_at without a live-table PK migration. No behavioral change.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, primary_key=True
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ScheduledRunRow(Base):
    """Per-tick dedup ledger for the recurring-task scheduler.

    Composite PK `(schedule_id, fire_time)` is the unique-target the
    `INSERT … ON CONFLICT DO NOTHING` claim races against — exactly one
    worker inserts the row for a normalized fire slot, and only that
    worker enqueues. `fire_time` is the cron slot floored to the minute
    so concurrent workers all race the same row.
    """

    __tablename__ = "scheduled_runs"

    schedule_id: Mapped[str] = mapped_column(String, primary_key=True)
    fire_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
