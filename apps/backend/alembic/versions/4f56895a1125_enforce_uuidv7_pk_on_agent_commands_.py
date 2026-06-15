"""enforce uuidv7 PK on agent_commands + workspaces via CHECK

Revision ID: 4f56895a1125
Revises: 16c46c01359d
Create Date: 2026-06-15 00:54:31.683281

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4f56895a1125"
down_revision: str | Sequence[str] | None = "16c46c01359d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add a CHECK that the PK is a UUIDv7 on the two tables whose id is minted
    app-side by command producers.

    `agent_commands.id` is the FIFO claim sort key (`claim_next` orders by `id`),
    so a random uuid4 PK scrambles delivery order; `workspaces.id` is the agent's
    lifecycle handle. Both are minted app-side with `uuid7()`, overriding the
    column's `server_default=text("uuidv7()")`. The semgrep taint rule cannot see
    that override because the mint and the `Row(id=...)` insert sit in different
    functions (producer DTO → enqueue_command / agent_report), so this constraint
    is the authoritative guard — it fails any INSERT that supplies a non-v7 id.

    Added `NOT VALID`: existing rows from before this guard (already-dispatched
    commands carrying uuid4 ids) are grandfathered, while every new INSERT is
    checked. Re-runnable after a partial failure via the pg_constraint guard.
    """
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'ck_agent_commands_id_uuidv7'
                ) THEN
                    ALTER TABLE agent_commands
                        ADD CONSTRAINT ck_agent_commands_id_uuidv7
                        CHECK (uuid_extract_version(id) = 7) NOT VALID;
                END IF;
            END $$;
            """
        )
    )
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'ck_workspaces_id_uuidv7'
                ) THEN
                    ALTER TABLE workspaces
                        ADD CONSTRAINT ck_workspaces_id_uuidv7
                        CHECK (uuid_extract_version(id) = 7) NOT VALID;
                END IF;
            END $$;
            """
        )
    )


def downgrade() -> None:
    """Drop the UUIDv7 CHECK constraints."""
    op.execute(sa.text("ALTER TABLE agent_commands DROP CONSTRAINT IF EXISTS ck_agent_commands_id_uuidv7"))
    op.execute(sa.text("ALTER TABLE workspaces DROP CONSTRAINT IF EXISTS ck_workspaces_id_uuidv7"))
