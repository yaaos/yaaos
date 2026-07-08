"""drop claude_code_repos

The per-repo skill-name mapping was the retired reviewer flow's skill
selector; a pipeline stage's own `skill_name` picks the skill now, and no
dispatch path reads this table.

Revision ID: 71e9f0d26017
Revises: 9c1a2b3d4e5f
Create Date: 2026-07-08 18:14:40.744987

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "71e9f0d26017"
down_revision: str | Sequence[str] | None = "9c1a2b3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS claude_code_repos"))


def downgrade() -> None:
    op.create_table(
        "claude_code_repos",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("repo_external_id", sa.VARCHAR(), nullable=False),
        sa.Column("skill_name", sa.VARCHAR(), nullable=True),
        sa.Column(
            "created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="claude_code_repos_pkey"),
        sa.UniqueConstraint("org_id", "repo_external_id", name="uq_claude_code_repos_org_repo"),
    )
