"""drop schema_migrations + ix_tickets_idempotency_key, add mcp_review_tokens fk

Revision ID: 16c46c01359d
Revises: 0001_baseline
Create Date: 2026-06-13 13:22:20.378678

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "16c46c01359d"
down_revision: str | Sequence[str] | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Three drifts the model metadata declared that the DB hadn't caught up to:

    1. `schema_migrations` table — a relic from a pre-Alembic tracking table.
       Alembic owns version tracking via `alembic_version`; the old table is
       dead weight.
    2. `mcp_review_tokens.review_id` FK to `reviews(id)` ON DELETE CASCADE —
       declared in the model but never materialized in the DB.
    3. `ix_tickets_idempotency_key` (partial-unique index on
       `tickets.idempotency_key WHERE idempotency_key IS NOT NULL`) — removed
       from the model when the idempotency story moved elsewhere; the index
       remained behind.
    """
    # Idempotent: safe on both old DBs (carrying the relics + an unnamed FK)
    # and fresh DBs built from 0001_baseline (no relics; FK already created
    # under the canonical name). Per CLAUDE.md "Idempotent migrations" rule.
    op.execute(sa.text("DROP TABLE IF EXISTS schema_migrations"))
    op.execute(
        sa.text("ALTER TABLE mcp_review_tokens DROP CONSTRAINT IF EXISTS mcp_review_tokens_review_id_fkey")
    )
    op.create_foreign_key(
        "mcp_review_tokens_review_id_fkey",
        "mcp_review_tokens",
        "reviews",
        ["review_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.execute(sa.text("DROP INDEX IF EXISTS ix_tickets_idempotency_key"))


def downgrade() -> None:
    """Downgrade schema."""
    op.create_index(
        op.f("ix_tickets_idempotency_key"),
        "tickets",
        ["idempotency_key"],
        unique=True,
        postgresql_where="(idempotency_key IS NOT NULL)",
    )
    op.drop_constraint(
        "mcp_review_tokens_review_id_fkey",
        "mcp_review_tokens",
        type_="foreignkey",
    )
    op.create_table(
        "schema_migrations",
        sa.Column("version", sa.TEXT(), autoincrement=False, nullable=False),
        sa.Column(
            "applied_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            autoincrement=False,
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("version", name=op.f("schema_migrations_pkey")),
    )
