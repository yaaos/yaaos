"""Add mcp_server OAuth tables.

Four new tables for the inbound MCP authorization-server surface:
  mcp_oauth_clients   — dynamic client registrations (RFC 7591)
  mcp_auth_codes      — one-time authorization codes (10-minute TTL)
  mcp_access_tokens   — opaque bearer tokens (hours-scale TTL)
  mcp_refresh_tokens  — rotation tokens (weeks-scale TTL)

All follow the bearer-discipline pattern: token_hash TEXT PK, raw token never
persists.  Expired rows are swept by the hourly scheduled task.

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-07-13 00:00:00.000000
"""

from __future__ import annotations

from sqlalchemy import text

from alembic import op

revision = "c6d7e8f9a0b1"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text("""
        CREATE TABLE IF NOT EXISTS mcp_oauth_clients (
            client_id       UUID        PRIMARY KEY DEFAULT uuidv7(),
            client_name     TEXT        NOT NULL,
            redirect_uris   JSONB       NOT NULL DEFAULT '[]',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """)
    )

    op.execute(
        text("""
        CREATE TABLE IF NOT EXISTS mcp_auth_codes (
            code_hash        TEXT        PRIMARY KEY,
            client_id        UUID        NOT NULL REFERENCES mcp_oauth_clients(client_id) ON DELETE CASCADE,
            user_id          UUID        NOT NULL,
            org_id           UUID        NOT NULL,
            code_challenge   TEXT        NOT NULL,
            redirect_uri     TEXT        NOT NULL,
            expires_at       TIMESTAMPTZ NOT NULL,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """)
    )

    op.execute(
        text("""
        CREATE TABLE IF NOT EXISTS mcp_access_tokens (
            token_hash   TEXT        PRIMARY KEY,
            client_id    UUID        NOT NULL REFERENCES mcp_oauth_clients(client_id) ON DELETE CASCADE,
            user_id      UUID        NOT NULL,
            org_id       UUID        NOT NULL,
            expires_at   TIMESTAMPTZ NOT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """)
    )

    op.execute(
        text("""
        CREATE INDEX IF NOT EXISTS idx_mcp_access_tokens_user
            ON mcp_access_tokens (user_id)
        """)
    )

    op.execute(
        text("""
        CREATE TABLE IF NOT EXISTS mcp_refresh_tokens (
            token_hash   TEXT        PRIMARY KEY,
            client_id    UUID        NOT NULL REFERENCES mcp_oauth_clients(client_id) ON DELETE CASCADE,
            user_id      UUID        NOT NULL,
            org_id       UUID        NOT NULL,
            expires_at   TIMESTAMPTZ NOT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """)
    )

    op.execute(
        text("""
        CREATE INDEX IF NOT EXISTS idx_mcp_refresh_tokens_user
            ON mcp_refresh_tokens (user_id)
        """)
    )


def downgrade() -> None:
    op.execute(text("DROP TABLE IF EXISTS mcp_refresh_tokens"))
    op.execute(text("DROP TABLE IF EXISTS mcp_access_tokens"))
    op.execute(text("DROP TABLE IF EXISTS mcp_auth_codes"))
    op.execute(text("DROP TABLE IF EXISTS mcp_oauth_clients"))
