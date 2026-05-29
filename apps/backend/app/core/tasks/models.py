"""SQLAlchemy model for `outbox_entries` — the pending-dispatch table.

Folded in from the former `core/outbox` module. The table name stays
`outbox_entries` since migration 014 already shipped under it; the row
shape is generic (`kind` / `payload` / `attempt` / `dispatched_at`) so
new kinds plug in via dispatchers without schema changes.

Today the only `kind` is `taskiq_enqueue`. The row is a private detail
of `core/tasks`; callers see only the `enqueue()` API.
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
