"""SQLAlchemy models for `core/tenancy` — orgs and memberships tables.

These two tables form the IAM access graph: who is in which org at what role.
`core/tenancy` owns the tables; `domain/orgs` owns invitation and SSO feature
rows that reference them.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class OrgRow(Base):
    __tablename__ = "orgs"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False, default="")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Per-org idle session timeout (minutes). Null = use the global default.
    session_timeout_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Chosen VCS plugin (one per org). `vcs_settings` carries plugin-specific config.
    vcs_plugin_id: Mapped[str | None] = mapped_column(String, nullable=True)
    vcs_settings: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # per-org workspace provider selection. `in_memory` (default)
    # runs workspaces in-process; `remote_agent` dispatches to a customer-
    # deployed WorkspaceAgent via `core/agent_gateway`. `registered_iam_arn` is
    # the canonical IAM role ARN the customer registered; the identity-exchange
    # verifier canonicalizes the assumed-role ARN it gets back from STS and
    # matches against this column. `aws_region` pins the STS endpoint the
    # signed request must target — defence against cross-region replay.
    # registered_iam_arn and aws_region are both-or-neither (DB check
    # constraint `ck_orgs_arn_region_paired`).
    workspace_provider: Mapped[str | None] = mapped_column(String, nullable=True)
    # Uniqueness on `registered_iam_arn` is enforced by a partial unique index
    # (`uq_orgs_registered_iam_arn`) created in migration 028, not by the model
    # — so in-memory orgs (NULL ARN) don't collide and aren't capped at one row.
    registered_iam_arn: Mapped[str | None] = mapped_column(String, nullable=True)
    aws_region: Mapped[str | None] = mapped_column(String, nullable=True)
    # SSO authz flags denormalized from sso_configs for fast middleware access.
    # `sso_enabled` mirrors `sso_configs.enabled`; `sso_exempt_owner_user_id`
    # mirrors `sso_configs.exempt_owner_user_id`. Backfilled by migration 034.
    sso_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    sso_exempt_owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MembershipRow(Base):
    __tablename__ = "memberships"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), primary_key=True
    )
    # Three-enum role. Stored as a string; the Python enum lives in service.py.
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    # `@handle` is per-membership, not per-user — a user can be @jack here and
    # @jkora there.
    handle: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("org_id", "handle", name="uq_membership_org_handle"),
        Index("ix_memberships_org_role", "org_id", "role"),
    )
