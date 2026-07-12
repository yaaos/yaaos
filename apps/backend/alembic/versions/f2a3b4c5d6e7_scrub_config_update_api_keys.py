"""Scrub api_keys from persisted ConfigUpdate command rows.

Credentials must not rest in agent_commands.payload. This migration removes
the config.api_keys subtree from every ConfigUpdate row. Rows without that key
are unaffected (JSONB path removal is a no-op when the path is absent).

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-07-12 00:00:00.000000

"""

from __future__ import annotations

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision = "f2a3b4c5d6e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Remove the config.api_keys subtree from every ConfigUpdate command row.
    # The JSONB `#-` operator removes the element at the given path; when the
    # path does not exist the expression returns the original value unchanged,
    # making this statement inherently idempotent.
    op.execute(
        text(
            "UPDATE agent_commands "
            "SET payload = payload #- '{config,api_keys}' "
            "WHERE command_kind = 'ConfigUpdate'"
        )
    )


def downgrade() -> None:
    # Credentials are not stored in the downgrade direction — the scrub is
    # irreversible by design (no plaintext was ever stored after the enqueue
    # path was updated to omit api_keys). No-op downgrade is intentional.
    pass
