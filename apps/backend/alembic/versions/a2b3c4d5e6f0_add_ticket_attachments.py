"""Add ticket_attachments table.

Revision ID: a2b3c4d5e6f0
Revises: b4c5d6e7f8a9
Create Date: 2026-07-13 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2b3c4d5e6f0"
down_revision: str | Sequence[str] | None = "b4c5d6e7f8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ticket_attachments table.

    `ticket_attachments` stores user-supplied ticket inputs (text documents
    attached before or during a run). Frontmatter metadata columns are NULL
    when the body carries no frontmatter (context-only attachment). The table
    is owned by `domain/attachments`.

    All DDL uses IF NOT EXISTS / IF EXISTS guards so re-running a partially
    applied migration is always safe.
    """
    op.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS ticket_attachments (
                id                  UUID PRIMARY KEY DEFAULT uuidv7(),
                org_id              UUID NOT NULL,
                ticket_id           UUID NOT NULL
                    REFERENCES tickets(id) ON DELETE CASCADE,
                filename            TEXT NOT NULL,
                body                TEXT NOT NULL,
                produced_by_skill   TEXT,
                skill_version       TEXT,
                artifact_type       TEXT,
                produced_at         TIMESTAMPTZ,
                repo_commit         TEXT,
                produced_from       TEXT,
                note                TEXT,
                attached_by         UUID NOT NULL,
                attached_at         TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS idx_ticket_attachments_match
                ON ticket_attachments (ticket_id, produced_by_skill, attached_at DESC);
            """
        )
    )


def downgrade() -> None:
    """Drop ticket_attachments table and its index."""
    op.execute(sa.text("DROP TABLE IF EXISTS ticket_attachments CASCADE"))
