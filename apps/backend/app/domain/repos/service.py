"""Stub service surface for `domain/repos`.

Bodies raise `NotImplementedError` — only the signatures are load-bearing.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor
from app.domain.repos.types import (
    DueFire,
    ProtectedMatch,
    ProtectedPathSet,
    RepoConfigSummary,
    RepoSettings,
    RepoSettingsSpec,
    TriggerBinding,
    TriggerBindingSpec,
)


async def get_settings(org_id: UUID, repo_external_id: str, *, session: AsyncSession) -> RepoSettings:
    raise NotImplementedError


async def put_settings(
    org_id: UUID,
    repo_external_id: str,
    *,
    settings: RepoSettingsSpec,
    actor: Actor,
    session: AsyncSession,
) -> None:
    """Whole-section replace (last-write-wins); validates glob compilability
    + conditions shape; audit `repo.settings_updated`."""
    raise NotImplementedError


async def list_repo_configs(org_id: UUID, *, session: AsyncSession) -> list[RepoConfigSummary]:
    """Config rows only — the web handler joins `vcs.list_installation_repos`
    for the full accordion."""
    raise NotImplementedError


async def add_binding(
    org_id: UUID,
    repo_external_id: str,
    *,
    spec: TriggerBindingSpec,
    actor: Actor,
    session: AsyncSession,
) -> UUID:
    raise NotImplementedError


async def remove_binding(binding_id: UUID, *, actor: Actor, session: AsyncSession) -> None:
    raise NotImplementedError


async def find_bindings(
    org_id: UUID, repo_external_id: str, intake_point_id: str, *, session: AsyncSession
) -> list[TriggerBinding]:
    raise NotImplementedError


async def evaluate_protected(
    org_id: UUID, repo_external_id: str, paths: Sequence[str], *, session: AsyncSession
) -> ProtectedMatch:
    """The engine's one-call boundary read; composes `get_settings` + `match_protected`."""
    raise NotImplementedError


def match_protected(
    paths: Sequence[str], *, mode: Literal["allow", "deny"], path_sets: Sequence[ProtectedPathSet]
) -> ProtectedMatch:
    """Pure, unit-testable path-matching rule."""
    raise NotImplementedError


async def pipeline_referenced_by_binding(pipeline_id: UUID, *, session: AsyncSession) -> bool:
    raise NotImplementedError


async def list_due_schedule_bindings(*, now: datetime, session: AsyncSession) -> list[DueFire]:
    """Cron matching (UTC, floored-minute slot) over schedule bindings.
    Consumed by `domain/pipelines`' `pipeline_schedule_tick`."""
    raise NotImplementedError
