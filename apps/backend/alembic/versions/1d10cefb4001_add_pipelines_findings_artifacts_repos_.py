"""add pipelines findings artifacts repos pr_review tables

Revision ID: 1d10cefb4001
Revises: d5e6f7a8b9c0
Create Date: 2026-07-07 05:01:11.672429

Adds the nine tables for the pipelines run-engine plus the two additive
`tickets` columns (`branch_name`, `current_run_id`). `core/workflow` +
`domain/reviewer` (and their `workflow_executions` / `pending_human_decisions`
/ `reviews` / `findings` tables) are untouched — this revision only adds. The
run-engine's finding table is named `pipeline_findings`, not `findings`,
because `domain/reviewer` owns a `findings` table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1d10cefb4001"
down_revision: str | Sequence[str] | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the nine new tables (in FK-dependency order) + two tickets columns."""
    op.create_table(
        "pipelines",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("stages", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"]),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "name", name="uq_pipelines_org_name"),
    )

    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("ticket_id", sa.UUID(), nullable=False),
        sa.Column("pipeline_id", sa.UUID(), nullable=True),
        sa.Column("pipeline_name", sa.String(), nullable=False),
        sa.Column("definition_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("phase", sa.String(), server_default="provision", nullable=False),
        sa.Column("current_stage_index", sa.Integer(), nullable=True),
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("pending_agent_command_id", sa.UUID(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("kickoff", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "sendback_counts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("otel_trace_context", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("phase IN ('provision','stages','cleanup')", name="ck_pipeline_runs_phase"),
        sa.CheckConstraint(
            "state IN ('queued','running','paused','completed','failed','killed','cancelled')",
            name="ck_pipeline_runs_state",
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"]),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pipeline_runs_state", "pipeline_runs", ["state"], unique=False)
    op.create_index("ix_pipeline_runs_ticket", "pipeline_runs", ["ticket_id"], unique=False)
    op.create_index(
        "ux_pipeline_runs_one_in_flight",
        "pipeline_runs",
        ["ticket_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('running','paused')"),
    )

    op.create_table(
        "stage_executions",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("stage_index", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("stage_name", sa.String(), nullable=False),
        sa.Column("skill_name", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("phase", sa.String(), nullable=True),
        sa.Column("iteration", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("confidence", sa.String(), nullable=True),
        sa.Column(
            "loop_state",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column("boundary_outcome", sa.String(), nullable=True),
        sa.Column("boundary_detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("action_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("revision", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "boundary_outcome IS NULL OR boundary_outcome IN ('proceeded','paused','sent_back')",
            name="ck_stage_executions_boundary_outcome",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR confidence IN ('low','medium','high')",
            name="ck_stage_executions_confidence",
        ),
        sa.CheckConstraint("kind IN ('skill','review','action','system')", name="ck_stage_executions_kind"),
        sa.CheckConstraint(
            "phase IS NULL OR phase IN ('main','review','fix')", name="ck_stage_executions_phase"
        ),
        sa.CheckConstraint("status IN ('running','completed','failed')", name="ck_stage_executions_status"),
        sa.ForeignKeyConstraint(["run_id"], ["pipeline_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_stage_executions_run", "stage_executions", ["run_id", "stage_index"], unique=False)

    op.create_table(
        "artifacts",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("ticket_id", sa.UUID(), nullable=False),
        sa.Column("stage_name", sa.String(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("stage_execution_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("iteration", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_final", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["pipeline_runs.id"]),
        sa.ForeignKeyConstraint(["stage_execution_id"], ["stage_executions.id"]),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ticket_id", "stage_name", "version", name="uq_artifacts_lineage_version"),
    )
    op.create_index(
        "ix_artifacts_lineage_final", "artifacts", ["ticket_id", "stage_name", "is_final"], unique=False
    )

    op.create_table(
        "pipeline_findings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("ticket_id", sa.UUID(), nullable=False),
        sa.Column("source_run_id", sa.UUID(), nullable=False),
        sa.Column("source_stage_name", sa.String(), nullable=False),
        sa.Column("source_stage_execution_id", sa.UUID(), nullable=False),
        sa.Column("first_seen_iteration", sa.Integer(), nullable=False),
        sa.Column("display_prefix", sa.String(), nullable=False),
        sa.Column("display_id", sa.Integer(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("code_file", sa.String(), nullable=True),
        sa.Column("code_line", sa.Integer(), nullable=True),
        sa.Column("artifact_section", sa.String(), nullable=True),
        sa.Column("defect_in_artifact", sa.String(), nullable=True),
        sa.Column("status", sa.String(), server_default="open", nullable=False),
        sa.Column(
            "status_events",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column("defended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("external_comment_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "severity IN ('blocker','should_fix','nit')", name="ck_pipeline_findings_severity"
        ),
        sa.CheckConstraint("status IN ('open','resolved','dismissed')", name="ck_pipeline_findings_status"),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ticket_id", "display_id", name="uq_pipeline_findings_ticket_display_id"),
    )
    op.create_index(
        "ix_pipeline_findings_external_comment",
        "pipeline_findings",
        ["org_id", "external_comment_id"],
        unique=False,
    )
    op.create_index(
        "ix_pipeline_findings_stage_execution",
        "pipeline_findings",
        ["source_stage_execution_id"],
        unique=False,
    )
    op.create_index(
        "ix_pipeline_findings_ticket_status", "pipeline_findings", ["ticket_id", "status"], unique=False
    )

    op.create_table(
        "repo_settings",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("repo_external_id", sa.String(), nullable=False),
        sa.Column("protected_mode", sa.String(), server_default="deny", nullable=False),
        sa.Column(
            "protected_path_sets",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column("auto_approve_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "auto_approve_conditions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.CheckConstraint("protected_mode IN ('allow','deny')", name="ck_repo_settings_protected_mode"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("org_id", "repo_external_id", name="pk_repo_settings"),
    )

    op.create_table(
        "repo_trigger_bindings",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("repo_external_id", sa.String(), nullable=False),
        sa.Column("intake_point_id", sa.String(), nullable=False),
        sa.Column("pipeline_id", sa.UUID(), nullable=False),
        sa.Column("schedule", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["pipeline_id"], ["pipelines.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bindings_repo", "repo_trigger_bindings", ["org_id", "repo_external_id"], unique=False)
    op.create_index(
        "ux_bindings_point",
        "repo_trigger_bindings",
        ["org_id", "repo_external_id", "intake_point_id"],
        unique=True,
        postgresql_where=sa.text("schedule IS NULL"),
    )

    op.create_table(
        "pr_comments",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("ticket_id", sa.UUID(), nullable=False),
        sa.Column("comment_external_id", sa.String(), nullable=False),
        sa.Column("in_reply_to_external_id", sa.String(), nullable=True),
        sa.Column("author_login", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("finding_id", sa.UUID(), nullable=True),
        sa.Column("classification", sa.String(), nullable=True),
        sa.Column("claimed_by_run_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "classification IS NULL OR classification IN ('question','claims_fixed','dispute','unclear')",
            name="ck_pr_comments_classification",
        ),
        sa.ForeignKeyConstraint(["finding_id"], ["pipeline_findings.id"]),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "comment_external_id", name="uq_pr_comments_org_external"),
    )
    op.create_index(
        "ix_pr_comments_waiting",
        "pr_comments",
        ["ticket_id"],
        unique=False,
        postgresql_where=sa.text(
            "claimed_by_run_id IS NULL AND classification IS NOT NULL AND classification != 'unclear'"
        ),
    )

    op.create_table(
        "run_pauses",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("stage_execution_id", sa.UUID(), nullable=False),
        sa.Column("tripped", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("escalation_user_ids", postgresql.ARRAY(sa.UUID()), nullable=False),
        sa.Column("resolution", sa.String(), nullable=True),
        sa.Column("instruction", sa.Text(), nullable=True),
        sa.Column("send_back_to_stage", sa.String(), nullable=True),
        sa.Column("resolved_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "resolution IS NULL OR resolution IN ('approve','instruct','send_back','kill')",
            name="ck_run_pauses_resolution",
        ),
        sa.ForeignKeyConstraint(["resolved_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["pipeline_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["stage_execution_id"], ["stage_executions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_run_pauses_run_open",
        "run_pauses",
        ["run_id"],
        unique=False,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )

    # Additive tickets columns — guarded for safe re-run (see
    # patterns.md § Idempotent migrations).
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tickets' AND column_name='current_run_id'
                ) THEN
                    ALTER TABLE tickets ADD COLUMN current_run_id UUID NULL;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tickets' AND column_name='branch_name'
                ) THEN
                    ALTER TABLE tickets ADD COLUMN branch_name VARCHAR NULL;
                END IF;
            END $$;
            """
        )
    )


def downgrade() -> None:
    """Drop the nine new tables + two tickets columns."""
    op.execute(sa.text("ALTER TABLE tickets DROP COLUMN IF EXISTS branch_name"))
    op.execute(sa.text("ALTER TABLE tickets DROP COLUMN IF EXISTS current_run_id"))

    op.drop_index(
        "ix_run_pauses_run_open", table_name="run_pauses", postgresql_where=sa.text("resolved_at IS NULL")
    )
    op.drop_table("run_pauses")

    op.drop_index(
        "ix_pr_comments_waiting",
        table_name="pr_comments",
        postgresql_where=sa.text(
            "claimed_by_run_id IS NULL AND classification IS NOT NULL AND classification != 'unclear'"
        ),
    )
    op.drop_table("pr_comments")

    op.drop_index(
        "ux_bindings_point", table_name="repo_trigger_bindings", postgresql_where=sa.text("schedule IS NULL")
    )
    op.drop_index("ix_bindings_repo", table_name="repo_trigger_bindings")
    op.drop_table("repo_trigger_bindings")

    op.drop_table("repo_settings")

    op.drop_index("ix_pipeline_findings_ticket_status", table_name="pipeline_findings")
    op.drop_index("ix_pipeline_findings_stage_execution", table_name="pipeline_findings")
    op.drop_index("ix_pipeline_findings_external_comment", table_name="pipeline_findings")
    op.drop_table("pipeline_findings")

    op.drop_index("ix_artifacts_lineage_final", table_name="artifacts")
    op.drop_table("artifacts")

    op.drop_index("ix_stage_executions_run", table_name="stage_executions")
    op.drop_table("stage_executions")

    op.drop_index(
        "ux_pipeline_runs_one_in_flight",
        table_name="pipeline_runs",
        postgresql_where=sa.text("state IN ('running','paused')"),
    )
    op.drop_index("ix_pipeline_runs_ticket", table_name="pipeline_runs")
    op.drop_index("ix_pipeline_runs_state", table_name="pipeline_runs")
    op.drop_table("pipeline_runs")

    op.drop_table("pipelines")
