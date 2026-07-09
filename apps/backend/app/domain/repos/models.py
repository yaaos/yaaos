"""SQLAlchemy rows owned by `domain/repos`.

There is no `repos` table — repos are external ids from the VCS
installation. `RepoSettingsRow` identity is `(org_id, repo_external_id)`; an
absent row means the model's defaults apply (`unconfigured` is a state, not
an error). `RepoTriggerBindingRow` rows carry `repo_external_id` only — the
VCS plugin is implied by the intake point's namespace.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class RepoSettingsRow(Base):
    """Per-repo protected-code + auto-approve config. Identity = (org_id,
    repo_external_id); absent row = the model's defaults."""

    __tablename__ = "repo_settings"

    org_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    repo_external_id: Mapped[str] = mapped_column(String, nullable=False)
    protected_mode: Mapped[str] = mapped_column(String, nullable=False, server_default="deny")
    protected_path_sets: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'")
    )
    auto_approve_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    auto_approve_conditions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    __table_args__ = (
        CheckConstraint("protected_mode IN ('allow','deny')", name="ck_repo_settings_protected_mode"),
        PrimaryKeyConstraint("org_id", "repo_external_id", name="pk_repo_settings"),
    )


class RepoTriggerBindingRow(Base):
    """One intake→pipeline binding for a repo. `schedule` is non-null iff the
    intake point's kind is `schedule`."""

    __tablename__ = "repo_trigger_bindings"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()"))
    org_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    repo_external_id: Mapped[str] = mapped_column(String, nullable=False)
    intake_point_id: Mapped[str] = mapped_column(String, nullable=False)
    pipeline_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("pipelines.id", ondelete="RESTRICT"), nullable=False
    )
    # `none_as_null=True` — otherwise SQLAlchemy's JSON types persist a
    # Python `None` as the JSON literal `null` (a non-NULL value), which
    # would silently defeat both `ux_bindings_point`'s `WHERE schedule IS
    # NULL` partial predicate and `add_binding`'s own duplicate-binding
    # pre-check (both need genuine SQL NULL on a non-schedule binding).
    schedule: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "ux_bindings_point",
            "org_id",
            "repo_external_id",
            "intake_point_id",
            unique=True,
            postgresql_where=text("schedule IS NULL"),
        ),
        Index("ix_bindings_repo", "org_id", "repo_external_id"),
    )
