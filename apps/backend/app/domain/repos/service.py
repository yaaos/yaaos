"""Service surface for `domain/repos`.

`get_settings`/`put_settings`/`match_protected`/`evaluate_protected` are
real — the boundary evaluator's one config read (`evaluate_protected`,
composing `get_settings` + `match_protected`) plus the write path behind
the Repos-page protected-code + auto-approve config.

`add_binding`/`remove_binding`/`find_bindings`/`list_repo_configs`/
`list_due_schedule_bindings` stay stubs — bodies raise `NotImplementedError`;
trigger bindings land with the intake-rewire phase. Exception:
`pipeline_referenced_by_binding` always returns `False` — no `TriggerBinding`
can reference a pipeline before repo trigger bindings are writable, so
`domain/pipelines.delete_pipeline` can call it safely today.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal
from uuid import UUID

import pathspec
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, audit_for_repo_settings
from app.domain.findings import AutoApproveConditions
from app.domain.repos.models import RepoSettingsRow
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


class InvalidProtectedGlobError(ValueError):
    """A `ProtectedPathSet.globs` entry doesn't compile as a gitignore-style pattern."""


class _RepoSettingsUpdatedPayload(BaseModel):
    repo_external_id: str
    protected_mode: str
    protected_path_set_count: int
    auto_approve_enabled: bool


async def get_settings(org_id: UUID, repo_external_id: str, *, session: AsyncSession) -> RepoSettings:
    row = await session.get(RepoSettingsRow, (org_id, repo_external_id))
    return RepoSettings.from_row(row)


def _validate_path_sets(path_sets: Sequence[ProtectedPathSet]) -> None:
    """Gitignore-style compilability check — `pathspec` raises on a
    malformed pattern (stdlib `fnmatch` mishandles `**`, hence the dep)."""
    for path_set in path_sets:
        for glob in path_set.globs:
            try:
                pathspec.GitIgnoreSpec.from_lines([glob])
            except ValueError as exc:
                raise InvalidProtectedGlobError(f"invalid glob {glob!r}: {exc}") from exc


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
    _validate_path_sets(settings.protected_path_sets)
    # Validate the auto-approve conditions dict against the Repos-owned
    # shape (findings-owned VO — see `apps/backend/docs/domain_findings.md`);
    # the row still stores the plain dict (`RepoSettingsSpec`'s own field
    # type), this only rejects a malformed body before it lands.
    AutoApproveConditions.model_validate(settings.auto_approve_conditions)

    row = await session.get(RepoSettingsRow, (org_id, repo_external_id))
    if row is None:
        row = RepoSettingsRow(org_id=org_id, repo_external_id=repo_external_id)
        session.add(row)
    row.protected_mode = settings.protected_mode
    row.protected_path_sets = [p.model_dump(mode="json") for p in settings.protected_path_sets]
    row.auto_approve_enabled = settings.auto_approve_enabled
    row.auto_approve_conditions = settings.auto_approve_conditions
    row.updated_by = actor.user_id
    await session.flush()

    await audit_for_repo_settings(
        org_id,
        "repo.settings_updated",
        _RepoSettingsUpdatedPayload(
            repo_external_id=repo_external_id,
            protected_mode=settings.protected_mode,
            protected_path_set_count=len(settings.protected_path_sets),
            auto_approve_enabled=settings.auto_approve_enabled,
        ),
        actor=actor,
        session=session,
    )


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
    settings = await get_settings(org_id, repo_external_id, session=session)
    return match_protected(paths, mode=settings.protected_mode, path_sets=settings.protected_path_sets)


def match_protected(
    paths: Sequence[str], *, mode: Literal["allow", "deny"], path_sets: Sequence[ProtectedPathSet]
) -> ProtectedMatch:
    """Pure, unit-testable path-matching rule.

    `deny`: matched iff any path hits any set; owners = the union of the
    owners of every set that matched at least one path.
    `allow`: matched iff any path escapes every set (hits none); owners =
    the union of ALL sets' owners — allow-mode with zero sets coherently
    protects everything (every path trivially escapes an empty rule list),
    with an empty owner set (base escalation only, since there's no set to
    own it). Empty `paths` never matches in either mode.
    """
    compiled = [(path_set, pathspec.GitIgnoreSpec.from_lines(path_set.globs)) for path_set in path_sets]

    if mode == "deny":
        matched_sets = [path_set for path_set, spec in compiled if any(spec.match_file(p) for p in paths)]
        if not matched_sets:
            return ProtectedMatch(matched=False, owner_user_ids=())
        owners = sorted({uid for path_set in matched_sets for uid in path_set.owner_user_ids}, key=str)
        return ProtectedMatch(matched=True, owner_user_ids=tuple(owners))

    escapes_every_set = any(not any(spec.match_file(p) for _, spec in compiled) for p in paths)
    if not escapes_every_set:
        return ProtectedMatch(matched=False, owner_user_ids=())
    owners = sorted({uid for path_set in path_sets for uid in path_set.owner_user_ids}, key=str)
    return ProtectedMatch(matched=True, owner_user_ids=tuple(owners))


async def pipeline_referenced_by_binding(pipeline_id: UUID, *, session: AsyncSession) -> bool:
    """Always False until repo trigger bindings exist — no `TriggerBinding`
    can reference a pipeline before the bindings themselves are writable."""
    del pipeline_id, session
    return False


async def list_due_schedule_bindings(*, now: datetime, session: AsyncSession) -> list[DueFire]:
    """Cron matching (UTC, floored-minute slot) over schedule bindings.
    Consumed by `domain/pipelines`' `pipeline_schedule_tick`."""
    raise NotImplementedError
