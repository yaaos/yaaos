"""Add adopted_from_attachment_id column to artifacts table.

Revision ID: b5c6d7e8f9a0
Revises: a2b3c4d5e6f0
Create Date: 2026-07-13 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5c6d7e8f9a0"
down_revision: str | Sequence[str] | None = "a2b3c4d5e6f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add adopted_from_attachment_id to artifacts.

    Nullable FK → ticket_attachments(id). NULL on engine-produced artifacts;
    set on artifacts whose body was synthesised directly from an attachment
    (adoption path in the run engine).

    Uses IF NOT EXISTS / conditional form so re-running a partially applied
    migration is always safe.
    """
    op.execute(
        sa.text(
            """
            ALTER TABLE artifacts
            ADD COLUMN IF NOT EXISTS adopted_from_attachment_id UUID
                REFERENCES ticket_attachments(id);
            """
        )
    )


def downgrade() -> None:
    """Drop the adopted_from_attachment_id column."""
    op.execute(
        sa.text(
            """
            ALTER TABLE artifacts
            DROP COLUMN IF EXISTS adopted_from_attachment_id;
            """
        )
    )
