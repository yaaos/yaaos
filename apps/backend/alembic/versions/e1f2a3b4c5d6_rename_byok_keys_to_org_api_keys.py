"""rename byok_keys to org_api_keys

Revision ID: e1f2a3b4c5d6
Revises: 71e9f0d26017
Create Date: 2026-07-09 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: str | Sequence[str] | None = "71e9f0d26017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Skip if already applied (idempotent guard — re-running after a partial
    # apply is always safe because the table already carries the new name).
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables"
            " WHERE table_schema = 'public' AND table_name = 'org_api_keys'"
        )
    ).scalar()
    if result:
        return

    op.execute(sa.text("ALTER TABLE byok_keys RENAME TO org_api_keys"))


def downgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables"
            " WHERE table_schema = 'public' AND table_name = 'byok_keys'"
        )
    ).scalar()
    if result:
        return

    op.execute(sa.text("ALTER TABLE org_api_keys RENAME TO byok_keys"))
