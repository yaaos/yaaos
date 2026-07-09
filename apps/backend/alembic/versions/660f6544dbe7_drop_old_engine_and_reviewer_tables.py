"""drop old engine and reviewer tables

Revision ID: 660f6544dbe7
Revises: 1d10cefb4001
Create Date: 2026-07-08 03:02:20.651012

Drops `core/workflow` (`workflow_executions`, `pending_human_decisions`) and
`domain/reviewer` (`reviews`, `findings`) wholesale — both modules are
deleted in this same change. `mcp_review_tokens.review_id` loses its FK to
`reviews.id` (becomes a soft reference, no DB constraint) so the column
survives the drop. `tickets.current_workflow_execution_id` is also dropped —
`tickets.current_run_id` is the sole run-correlation column from here on.

Irreversible by design: data in the dropped tables is discarded, and there is
nothing meaningful to reconstruct pre-production. `git revert` restores the
code; there is no matching data restore.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "660f6544dbe7"
down_revision: str | Sequence[str] | None = "1d10cefb4001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the old engine's + reviewer's tables and the now-dead ticket column."""
    # `mcp_review_tokens.review_id` no longer FKs `reviews.id` — it's a soft
    # reference now that `reviews` is gone. Drop the constraint before the
    # referenced table (the column + its data stay).
    op.execute(
        sa.text("ALTER TABLE mcp_review_tokens DROP CONSTRAINT IF EXISTS mcp_review_tokens_review_id_fkey")
    )

    # `findings.review_id` FKs `reviews.id` — drop findings first.
    op.execute(sa.text("DROP TABLE IF EXISTS findings"))
    op.execute(sa.text("DROP TABLE IF EXISTS reviews"))

    op.execute(sa.text("DROP TABLE IF EXISTS pending_human_decisions"))
    op.execute(sa.text("DROP TABLE IF EXISTS workflow_executions"))

    op.execute(sa.text("ALTER TABLE tickets DROP COLUMN IF EXISTS current_workflow_execution_id"))


def downgrade() -> None:
    """Not reversible — dropped table data is discarded (pre-production)."""
    raise NotImplementedError("dropping the old engine + reviewer tables is not reversible")
