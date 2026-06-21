"""add typed columns to workflow_executions

Moves engine-internal state out of step_state JSONB magic keys into typed
columns. Six new columns:
  - finalizer_fired BOOLEAN NOT NULL DEFAULT FALSE
  - step_attempts JSONB NOT NULL DEFAULT '{}'
  - recovered_steps JSONB NOT NULL DEFAULT '{}'
  - pending_failure_step_id TEXT
  - pending_failure_reason TEXT
  - workflow_input JSONB

In-flight rows are backfilled in two passes: first the column values are
written from the JSONB keys, then the nine engine-internal keys are stripped
from step_state. Per-step output keys (step ids like 'check', 'provision',
etc.) are never touched.

Revision ID: b3c4d5e6f7a8
Revises: c4dd2164033f
Create Date: 2026-06-19 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision: str = "b3c4d5e6f7a8"
down_revision: str | None = "c4dd2164033f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Add the six typed columns ──────────────────────────────────────────
    op.add_column(
        "workflow_executions",
        sa.Column("finalizer_fired", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
    )
    op.add_column(
        "workflow_executions",
        sa.Column(
            "step_attempts",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.add_column(
        "workflow_executions",
        sa.Column(
            "recovered_steps",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.add_column(
        "workflow_executions",
        sa.Column("pending_failure_step_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "workflow_executions",
        sa.Column("pending_failure_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "workflow_executions",
        sa.Column(
            "workflow_input",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )

    # ── Backfill columns from existing step_state JSONB keys ───────────────
    # Rows with non-empty step_state may carry any of these engine-internal
    # keys. Extract them into the typed columns before stripping.
    #
    # Note: (step_state->>'__finalizer_fired__')::boolean coerces 'true' string
    # to TRUE; COALESCE provides the DEFAULT FALSE for absent keys.
    # JSONB key absence returns NULL for ->> and JSON NULL for ->; COALESCE
    # handles both.
    op.execute(
        text("""
UPDATE workflow_executions
SET
    finalizer_fired = COALESCE(
        (step_state->>'__finalizer_fired__')::boolean,
        FALSE
    ),
    step_attempts = COALESCE(
        step_state->'__attempts__',
        '{}'::jsonb
    ),
    recovered_steps = COALESCE(
        step_state->'__recovered_steps__',
        '{}'::jsonb
    ),
    pending_failure_step_id = step_state->>'__pending_failure_step__',
    pending_failure_reason   = step_state->>'__pending_failure_reason__',
    workflow_input           = step_state->'__ticket_payload__'
WHERE step_state <> '{}'::jsonb
""")
    )

    # ── Strip the nine engine-internal keys from step_state ────────────────
    # The JSONB `-` operator is idempotent: removing an absent key is a no-op.
    # Step-output keys (step ids like 'check', 'provision', 'review', etc.)
    # are NEVER touched — only the nine double-underscore control keys below.
    op.execute(
        text("""
UPDATE workflow_executions
SET step_state =
    step_state
    - '__finalizer_fired__'
    - '__attempts__'
    - '__recovered_steps__'
    - '__append_queue__'
    - '__appended_pool__'
    - '__after_append__'
    - '__ticket_payload__'
    - '__pending_failure_step__'
    - '__pending_failure_reason__'
WHERE step_state <> '{}'::jsonb
""")
    )


def downgrade() -> None:
    op.drop_column("workflow_executions", "workflow_input")
    op.drop_column("workflow_executions", "pending_failure_reason")
    op.drop_column("workflow_executions", "pending_failure_step_id")
    op.drop_column("workflow_executions", "recovered_steps")
    op.drop_column("workflow_executions", "step_attempts")
    op.drop_column("workflow_executions", "finalizer_fired")
