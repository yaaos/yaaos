"""Value objects for `domain/repos`."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

from app.domain.repos.models import RepoSettingsRow

ProtectedMode = Literal["allow", "deny"]


class ProtectedPathSet(BaseModel):
    """Gitignore-style glob set + owners, validated compilable at write."""

    id: UUID
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
    """One due schedule firing, as returned by `list_due_schedule_bindings`."""

    binding: TriggerBinding
    fire_time: datetime
