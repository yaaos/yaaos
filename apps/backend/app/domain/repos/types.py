"""Value objects for `domain/repos`."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.repos.models import RepoSettingsRow

ProtectedMode = Literal["allow", "deny"]


class ProtectedPathSet(BaseModel):
    """Gitignore-style glob set + owners, validated compilable at write."""

    id: UUID
    name: str = Field(default="", max_length=100)
    globs: tuple[str, ...]
    owner_user_ids: tuple[UUID, ...]


class Schedule(BaseModel):
    """A per-repo cron trigger binding."""

    name: str
    cron: str
    notify_user_ids: tuple[UUID, ...]
    kickoff_input: str | None = None


class ProtectedMatch(BaseModel):
    """The boundary's protected-code answer."""

    matched: bool
    owner_user_ids: tuple[UUID, ...]


class RepoSettings(BaseModel):
    """The stored (or defaulted) per-repo config."""

    protected_mode: ProtectedMode = "deny"
    protected_path_sets: tuple[ProtectedPathSet, ...] = ()
    auto_approve_enabled: bool = False
    auto_approve_conditions: dict[str, Any] = {}

    @classmethod
    def from_row(cls, row: RepoSettingsRow | None) -> RepoSettings:
        if row is None:
            return cls()
        return cls(
            protected_mode=row.protected_mode,  # type: ignore[arg-type]
            protected_path_sets=tuple(ProtectedPathSet.model_validate(p) for p in row.protected_path_sets),
            auto_approve_enabled=row.auto_approve_enabled,
            auto_approve_conditions=dict(row.auto_approve_conditions),
        )


class RepoSettingsSpec(BaseModel):
    """`RepoSettings` sans identity — the PUT request body shape."""

    protected_mode: ProtectedMode = "deny"
    protected_path_sets: tuple[ProtectedPathSet, ...] = ()
    auto_approve_enabled: bool = False
    auto_approve_conditions: dict[str, Any] = {}


class TriggerBinding(BaseModel):
    """One repo intake→pipeline binding."""

    id: UUID
    repo_external_id: str
    intake_point_id: str
    pipeline_id: UUID
    pipeline_name: str
    schedule: Schedule | None = None
    # Attribution: the yaaos user who created this binding.  For
    # schedule-kind bindings this propagates to `triggered_by_user_id` on
    # per-user-mode runs fired by the binding.
    created_by: UUID | None = None


class TriggerBindingSpec(BaseModel):
    """Write input for `add_binding`."""

    intake_point_id: str
    pipeline_id: UUID
    schedule: Schedule | None = None


class RepoConfigSummary(BaseModel):
    """One row in the Repos-page accordion (config side only)."""

    repo_external_id: str
    configured: bool
    trigger_count: int
    has_protected_code: bool
    auto_approve_enabled: bool


class DueFire(BaseModel):
    """One due schedule firing, as returned by `list_due_schedule_bindings`.

    `org_id` is not on `TriggerBinding` itself (that VO is also the read
    model for the Repos-page accordion, which is always called inside an
    already org-scoped request) — `list_due_schedule_bindings` is a global,
    cross-org scan, so the firing needs to carry its own org identity for
    the caller to open `org_context` and create the ticket in the right org.
    """

    org_id: UUID
    binding: TriggerBinding
    fire_time: datetime


class PipelineRef(BaseModel):
    """Minimal pipeline identity resolved via the registered pipeline
    lookup (see `register_pipeline_lookup` in `service.py`). `domain/repos`
    can't import `domain/pipelines` directly — `pipelines` already depends
    on `repos` (`pipeline_referenced_by_binding`), and the reverse edge
    would cycle — so `pipelines` hands `repos` a lookup callable at import
    time instead, mirroring `core/api_keys.register_validator`."""

    org_id: UUID
    name: str
