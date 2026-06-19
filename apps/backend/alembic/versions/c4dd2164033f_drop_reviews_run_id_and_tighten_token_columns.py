"""drop reviews.run_id and tighten token columns

Removes the write-only `reviews.run_id` FK (never read by production code).
Tightens `coding_agent_runs.tokens_in` / `.tokens_out` to NOT NULL DEFAULT 0
now that `finalize_run` always writes them (pre-existing NULL rows back-filled
to 0 before the constraint is applied).

Revision ID: c4dd2164033f
Revises: 5ea87cddfb6c
Create Date: 2026-06-16 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4dd2164033f"
down_revision: str | Sequence[str] | None = "5ea87cddfb6c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop reviews.run_id FK + column; tighten token columns to NOT NULL."""
    # Drop FK constraint first (required before dropping the column).
    op.execute(sa.text("ALTER TABLE reviews DROP CONSTRAINT IF EXISTS reviews_run_id_fkey"))
    op.execute(sa.text("ALTER TABLE reviews DROP COLUMN IF EXISTS run_id"))

    # Back-fill NULLs before adding NOT NULL constraint.
    op.execute(sa.text("UPDATE coding_agent_runs SET tokens_in = 0 WHERE tokens_in IS NULL"))
    op.execute(sa.text("UPDATE coding_agent_runs SET tokens_out = 0 WHERE tokens_out IS NULL"))

    # Tighten columns to NOT NULL with a DEFAULT so future inserts that omit
    # the column also satisfy the constraint.
    op.execute(
        sa.text(
            "ALTER TABLE coding_agent_runs"
            " ALTER COLUMN tokens_in SET NOT NULL,"
            " ALTER COLUMN tokens_in SET DEFAULT 0"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE coding_agent_runs"
            " ALTER COLUMN tokens_out SET NOT NULL,"
            " ALTER COLUMN tokens_out SET DEFAULT 0"
        )
    )


def downgrade() -> None:
    """Re-add reviews.run_id as nullable FK; revert token columns to nullable."""
    # Revert token columns to nullable (drop the NOT NULL + default).
    op.execute(
        sa.text(
            "ALTER TABLE coding_agent_runs"
            " ALTER COLUMN tokens_in DROP NOT NULL,"
            " ALTER COLUMN tokens_in DROP DEFAULT"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE coding_agent_runs"
            " ALTER COLUMN tokens_out DROP NOT NULL,"
            " ALTER COLUMN tokens_out DROP DEFAULT"
        )
    )

    # Re-add the nullable run_id column and FK.
    op.execute(sa.text("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS run_id UUID"))
    op.execute(
        sa.text(
            "ALTER TABLE reviews"
            " ADD CONSTRAINT reviews_run_id_fkey"
            " FOREIGN KEY (run_id) REFERENCES coding_agent_runs(id)"
        )
    )
