"""rename run-id correlation columns + rename pipeline_findings to findings

Finishes the vocabulary rename started when the fixed workflow engine was
replaced by the pipelines engine:

- `agent_commands.workflow_execution_id` -> `run_id`
- `coding_agent_runs.workflow_execution_id` -> `run_id`
- `coding_agent_runs.step_id` (TEXT) -> `stage_execution_id` (UUID) — rows
  written by the retired fixed workflow engine hold step *names* (e.g.
  "CodeReview") that reference tables dropped in 660f6544dbe7; they are
  deleted before the cast. Every surviving row is new-engine-written and
  holds a stringified uuid7.
- `tickets.branch_name` backfilled for any pre-branch-minting row, then set
  NOT NULL — every ticket has a work branch from here on.
- `pipeline_findings` -> `findings` (incl. indexes/constraints) — the name is
  free now that the old reviewer's `findings` table was dropped.

Revision ID: 9c1a2b3d4e5f
Revises: 660f6544dbe7
Create Date: 2026-07-08 05:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9c1a2b3d4e5f"
down_revision: str | Sequence[str] | None = "660f6544dbe7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── agent_commands.workflow_execution_id -> run_id ─────────────────────
    op.alter_column("agent_commands", "workflow_execution_id", new_column_name="run_id")

    # ── coding_agent_runs.workflow_execution_id -> run_id ───────────────────
    op.alter_column("coding_agent_runs", "workflow_execution_id", new_column_name="run_id")

    # ── coding_agent_runs.step_id (TEXT) -> stage_execution_id (UUID) ───────
    # Old-engine rows hold step names ("CodeReview") — orphans of the tables
    # dropped in 660f6544dbe7, not castable to uuid. Delete them; pipelines-
    # engine rows always hold a stringified uuid7, so the cast is then safe.
    op.execute(
        sa.text(
            "DELETE FROM coding_agent_runs WHERE step_id !~*"
            " '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'"
        )
    )
    op.execute(sa.text("ALTER TABLE coding_agent_runs ALTER COLUMN step_id TYPE uuid USING step_id::uuid"))
    op.alter_column("coding_agent_runs", "step_id", new_column_name="stage_execution_id")

    # ── tickets.branch_name backfill + NOT NULL ─────────────────────────────
    op.execute(
        sa.text(
            "UPDATE tickets SET branch_name = 'yaaos/ticket-' || left(id::text, 8) WHERE branch_name IS NULL"
        )
    )
    op.alter_column("tickets", "branch_name", nullable=False)

    # ── pipeline_findings -> findings ────────────────────────────────────────
    op.execute(sa.text("ALTER INDEX ix_pipeline_findings_ticket_status RENAME TO ix_findings_ticket_status"))
    op.execute(
        sa.text("ALTER INDEX ix_pipeline_findings_stage_execution RENAME TO ix_findings_stage_execution")
    )
    op.execute(
        sa.text("ALTER INDEX ix_pipeline_findings_external_comment RENAME TO ix_findings_external_comment")
    )
    op.execute(
        sa.text(
            "ALTER TABLE pipeline_findings RENAME CONSTRAINT ck_pipeline_findings_severity"
            " TO ck_findings_severity"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE pipeline_findings RENAME CONSTRAINT ck_pipeline_findings_status"
            " TO ck_findings_status"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE pipeline_findings RENAME CONSTRAINT uq_pipeline_findings_ticket_display_id"
            " TO uq_findings_ticket_display_id"
        )
    )
    op.execute(sa.text("ALTER TABLE pipeline_findings RENAME TO findings"))


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE findings RENAME TO pipeline_findings"))
    op.execute(
        sa.text(
            "ALTER TABLE pipeline_findings RENAME CONSTRAINT uq_findings_ticket_display_id"
            " TO uq_pipeline_findings_ticket_display_id"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE pipeline_findings RENAME CONSTRAINT ck_findings_status"
            " TO ck_pipeline_findings_status"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE pipeline_findings RENAME CONSTRAINT ck_findings_severity"
            " TO ck_pipeline_findings_severity"
        )
    )
    op.execute(
        sa.text("ALTER INDEX ix_findings_external_comment RENAME TO ix_pipeline_findings_external_comment")
    )
    op.execute(
        sa.text("ALTER INDEX ix_findings_stage_execution RENAME TO ix_pipeline_findings_stage_execution")
    )
    op.execute(sa.text("ALTER INDEX ix_findings_ticket_status RENAME TO ix_pipeline_findings_ticket_status"))

    op.alter_column("tickets", "branch_name", nullable=True)

    op.alter_column("coding_agent_runs", "stage_execution_id", new_column_name="step_id")
    op.execute(sa.text("ALTER TABLE coding_agent_runs ALTER COLUMN step_id TYPE varchar USING step_id::text"))

    op.alter_column("coding_agent_runs", "run_id", new_column_name="workflow_execution_id")

    op.alter_column("agent_commands", "run_id", new_column_name="workflow_execution_id")
