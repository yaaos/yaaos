"""add lifecycle column to workspace_agents

Revision ID: a1b2c3d4e5f6
Revises: b3c4d5e6f7a8
Create Date: 2026-06-23 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "b3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add `lifecycle` column to workspace_agents and backfill existing rows.

    The column tracks agent drain lifecycle: unconfigured → active → draining →
    shutdown.  It is orthogonal to the existing `state` column (liveness:
    reachable / stale / offline).

    Backfill: agents whose `state` is reachable or stale are already running
    and have processed a ConfigUpdate, so they get `active`.  Agents with any
    other state (offline) default to `unconfigured`.  The ADD COLUMN IF NOT
    EXISTS guard makes the migration re-runnable after a partial failure.
    """
    # ADD COLUMN IF NOT EXISTS guard — safe to re-run.
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='workspace_agents' AND column_name='lifecycle'
                ) THEN
                    ALTER TABLE workspace_agents
                        ADD COLUMN lifecycle TEXT NOT NULL DEFAULT 'unconfigured'
                        CHECK (lifecycle IN ('unconfigured', 'active', 'draining', 'shutdown'));
                END IF;
            END $$;
            """
        )
    )
    # Backfill: agents that are reachable or stale are actively running and
    # have a config applied — promote to 'active'.
    op.execute(
        sa.text(
            """
            UPDATE workspace_agents
            SET lifecycle = 'active'
            WHERE state IN ('reachable', 'stale')
              AND lifecycle = 'unconfigured';
            """
        )
    )


def downgrade() -> None:
    """Drop the lifecycle column."""
    op.execute(sa.text("ALTER TABLE workspace_agents DROP COLUMN IF EXISTS lifecycle"))
