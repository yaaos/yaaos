"""Add user_oauth_connections and user_oauth_device_sessions.

Revision ID: a2b3c4d5e6f7
Revises: f2a3b4c5d6e7
Create Date: 2026-07-12 01:00:00.000000

"""

from __future__ import annotations

from sqlalchemy import text

from alembic import op

revision = "a2b3c4d5e6f7"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS user_oauth_connections (
                user_id         UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                provider_id     TEXT        NOT NULL,
                status          TEXT        NOT NULL
                    CHECK (status IN ('connected', 'needs_reauth')),
                encrypted_access_token  TEXT        NOT NULL,
                encrypted_refresh_token TEXT        NULL,
                encrypted_id_token      TEXT        NULL,
                external_account_id     TEXT        NULL,
                granted_scope           TEXT        NULL,
                access_token_expires_at TIMESTAMPTZ NOT NULL,
                last_refresh_at         TIMESTAMPTZ NOT NULL,
                needs_reauth_reason     TEXT        NULL,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (user_id, provider_id)
            )
            """
        )
    )
    op.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_user_oauth_connections_refresh
            ON user_oauth_connections (last_refresh_at)
            WHERE status = 'connected'
            """
        )
    )
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS user_oauth_device_sessions (
                user_id                 UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                provider_id             TEXT        NOT NULL,
                encrypted_device_code   TEXT        NOT NULL,
                user_code               TEXT        NULL,
                verification_url        TEXT        NULL,
                poll_interval_seconds   INT         NOT NULL DEFAULT 5,
                expires_at              TIMESTAMPTZ NULL,
                created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (user_id, provider_id)
            )
            """
        )
    )


def downgrade() -> None:
    op.execute(text("DROP TABLE IF EXISTS user_oauth_device_sessions"))
    op.execute(text("DROP TABLE IF EXISTS user_oauth_connections"))
