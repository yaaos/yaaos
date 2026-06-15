"""add missing reviews columns

Adds columns referenced by the incremental-review trigger that were absent
from the model: scope_prev_sha, model, effort, current_step, skip_reason,
error_message, last_heartbeat_at, completed_at, pending_replay.

All columns are nullable so existing rows require no back-fill.

Revision ID: 5ea87cddfb6c
Revises: 4f56895a1125
Create Date: 2026-06-15 04:48:05.021210

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5ea87cddfb6c"
down_revision: str | Sequence[str] | None = "4f56895a1125"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add missing nullable columns to the reviews table.

    Uses ADD COLUMN IF NOT EXISTS so re-running after a partial failure is safe.
    """
    op.execute(sa.text("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS scope_prev_sha VARCHAR"))
    op.execute(sa.text("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS model VARCHAR"))
    op.execute(sa.text("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS effort VARCHAR"))
    op.execute(sa.text("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS current_step VARCHAR"))
    op.execute(sa.text("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS skip_reason VARCHAR"))
    op.execute(sa.text("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS error_message VARCHAR"))
    op.execute(sa.text("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ"))
    op.execute(sa.text("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ"))
    op.execute(sa.text("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS pending_replay BOOLEAN"))


def downgrade() -> None:
    """Drop the added reviews columns.

    Uses DROP COLUMN IF EXISTS so re-running after a partial failure is safe,
    mirroring the upgrade-path idempotency discipline.
    """
    for col in (
        "pending_replay",
        "completed_at",
        "last_heartbeat_at",
        "error_message",
        "skip_reason",
        "current_step",
        "effort",
        "model",
        "scope_prev_sha",
    ):
        op.execute(sa.text(f"ALTER TABLE reviews DROP COLUMN IF EXISTS {col}"))
