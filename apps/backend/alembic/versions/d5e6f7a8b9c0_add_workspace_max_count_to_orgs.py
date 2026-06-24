"""add workspace_max_count column to orgs

Revision ID: d5e6f7a8b9c0
Revises: a1b2c3d4e5f6
Create Date: 2026-06-23 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add `workspace_max_count` column to orgs.

    Per-org cap on concurrent Active workspaces for each WorkspaceAgent.  The
    value rides the `ConfigUpdate` AgentCommand to the agent, whose dispatch
    loop reads it as `cfg.MaxWorkspaces - activeCount` to gate new
    ProvisionWorkspace claims.  NOT NULL with a server default of 4 so existing
    rows backfill silently and new orgs get the same default as the prior
    hardcoded constant.  The CHECK constraint caps the user-facing value at 50
    (matches the PATCH endpoint's ge=1, le=50 validation) so a bad-faith write
    via SQL still fails loud.
    """
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='orgs' AND column_name='workspace_max_count'
                ) THEN
                    ALTER TABLE orgs
                        ADD COLUMN workspace_max_count INTEGER NOT NULL DEFAULT 4
                        CHECK (workspace_max_count BETWEEN 1 AND 50);
                END IF;
            END $$;
            """
        )
    )


def downgrade() -> None:
    """Drop the workspace_max_count column."""
    op.execute(sa.text("ALTER TABLE orgs DROP COLUMN IF EXISTS workspace_max_count"))
