"""Add run attribution columns.

`pipeline_runs.triggered_by_user_id` — the user whose credentials drive
per-user-mode coding-agent stages (PR author, rerun actor, or schedule
binding creator).  NULL on pre-existing rows and when attribution is
unresolvable (zero/multiple GitHub-username matches).

`repo_trigger_bindings.created_by` — the user who created the binding;
schedule-fired per-user runs run as this user.  NULL on pre-existing rows.

Revision ID: a3b4c5d6e7f8
Revises: a2b3c4d5e6f7
Create Date: 2026-07-12 00:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "a3b4c5d6e7f8"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pipeline_runs",
        sa.Column(
            "triggered_by_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "repo_trigger_bindings",
        sa.Column(
            "created_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("repo_trigger_bindings", "created_by")
    op.drop_column("pipeline_runs", "triggered_by_user_id")
