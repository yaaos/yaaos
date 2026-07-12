"""Add last_polled_at to user_oauth_device_sessions for server-side poll cooldown.

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-07-12 00:00:00.000000

"""

from __future__ import annotations

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision = "b4c5d6e7f8a9"
down_revision = "a3b4c5d6e7f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add last_polled_at column for server-side poll cooldown. Nullable so
    # existing rows (NULL = never polled) pass through cleanly; the cooldown
    # check in poll_device_auth treats NULL as "not yet polled".
    op.execute(
        text("ALTER TABLE user_oauth_device_sessions ADD COLUMN IF NOT EXISTS last_polled_at TIMESTAMPTZ")
    )


def downgrade() -> None:
    op.execute(text("ALTER TABLE user_oauth_device_sessions DROP COLUMN IF EXISTS last_polled_at"))
