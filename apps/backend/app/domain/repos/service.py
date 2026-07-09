"""Service surface for `domain/repos`.

`get_settings`/`put_settings`/`match_protected`/`evaluate_protected` are
real — the boundary evaluator's one config read (`evaluate_protected`,
composing `get_settings` + `match_protected`) plus the write path behind
the Repos-page protected-code + auto-approve config.

`add_binding`/`remove_binding`/`find_bindings`/`list_repo_configs`/
`pipeline_referenced_by_binding` are real — trigger bindings are writable.
`list_due_schedule_bindings` is real too — a cross-org cron match consumed by
`domain/pipelines.pipeline_schedule_tick`.

`add_binding`'s pipeline-org-ownership check can't import `domain/pipelines`
directly (`pipelines` already depends on `repos` for
`pipeline_referenced_by_binding`; the reverse edge would cycle). Instead
`domain/pipelines` registers a lookup callable at import time via
`register_pipeline_lookup`, mirroring `core/byok.register_validator`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Literal
from uuid import UUID

import pathspec
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, audit, audit_for_repo_settings
from app.core.auth import require_org_context
from app.core.intake import list_intake_points
from app.core.tasks import CronExpr
from app.core.tenancy import list_active_member_ids
from app.domain.findings import AutoApproveConditions
from app.domain.repos.models import RepoSettingsRow, RepoTriggerBindingRow
from app.domain.repos.types import (
    DueFire,
    PipelineRef,
    ProtectedMatch,
    ProtectedPathSet,
    RepoConfigSummary,
    RepoSettings,
    RepoSettingsSpec,
    Schedule,
    TriggerBinding,
    TriggerBindingSpec,
)


class InvalidProtectedGlobError(ValueError):
    """A `ProtectedPathSet.globs` entry doesn't compile as a gitignore-style pattern."""


class UnknownIntakePointError(ValueError):
    """`TriggerBindingSpec.intake_point_id` isn't registered."""


class InvalidScheduleError(ValueError):
    """`Schedule` is missing/present for the wrong intake-point kind, carries
    no `notify_user_ids`, or names a `notify_user_ids` entry outside the
    org's active membership."""


class InvalidCronError(ValueError):
    """`Schedule.cron` doesn't parse as a 5-field cron expression."""


class DuplicateBindingError(ValueError):
    """A non-schedule binding already exists for `(org, repo, intake_point)`."""


class UnknownPipelineError(ValueError):
    """`TriggerBindingSpec.pipeline_id` doesn't belong to the calling org."""


class BindingNotFoundError(LookupError):
    """No `repo_trigger_bindings` row for the given id in the current org."""


# Registered by `domain/pipelines` at import time — see module docstring.
_PipelineLookup = Callable[[UUID, AsyncSession], Awaitable[PipelineRef | None]]
_pipeline_lookup: _PipelineLookup | None = None


def register_pipeline_lookup(fn: _PipelineLookup) -> None:
    """Registered once, at `domain/pipelines` import time. Re-registering
    overwrites (mirrors `core/byok.register_validator`'s reload tolerance)."""
    global _pipeline_lookup
    _pipeline_lookup = fn


class _RepoSettingsUpdatedPayload(BaseModel):
    repo_external_id: str
    protected_mode: str
    protected_path_set_count: int
    auto_approve_enabled: bool


class _TriggerAddedPayload(BaseModel):
    repo_external_id: str
    intake_point_id: str
    pipeline_id: UUID


class _TriggerRemovedPayload(BaseModel):
    repo_external_id: str
    intake_point_id: str


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
    settings_rows = (
        (await session.execute(select(RepoSettingsRow).where(RepoSettingsRow.org_id == org_id)))
        .scalars()
        .all()
    )
    counts = dict(
        (
            await session.execute(
                select(RepoTriggerBindingRow.repo_external_id, func.count(RepoTriggerBindingRow.id))
                .where(RepoTriggerBindingRow.org_id == org_id)
                .group_by(RepoTriggerBindingRow.repo_external_id)
            )
        ).all()
    )
    settings_by_repo = {row.repo_external_id: row for row in settings_rows}
    repo_ids = set(counts) | set(settings_by_repo)

    out: list[RepoConfigSummary] = []
    for repo_external_id in sorted(repo_ids):
        row = settings_by_repo.get(repo_external_id)
        has_protected_code = row is not None and (
            row.protected_mode == "allow" or bool(row.protected_path_sets)
        )
        out.append(
            RepoConfigSummary(
                repo_external_id=repo_external_id,
                configured=row is not None,
                trigger_count=counts.get(repo_external_id, 0),
                has_protected_code=has_protected_code,
                auto_approve_enabled=bool(row and row.auto_approve_enabled),
            )
        )
    return out


def _validate_schedule(point_kind: str, schedule: Schedule | None, *, member_ids: set[UUID]) -> None:
    if point_kind == "schedule":
        if schedule is None:
            raise InvalidScheduleError("schedule is required for a schedule-kind intake point")
        if not schedule.notify_user_ids:
            raise InvalidScheduleError("schedule requires at least one notify_user_id")
        unknown = [uid for uid in schedule.notify_user_ids if uid not in member_ids]
        if unknown:
            raise InvalidScheduleError(f"notify_user_ids not in org membership: {unknown}")
        try:
            CronExpr.parse(schedule.cron)
        except ValueError as exc:
            raise InvalidCronError(str(exc)) from exc
    elif schedule is not None:
        raise InvalidScheduleError("schedule is only valid for a schedule-kind intake point")


async def add_binding(
    org_id: UUID,
    repo_external_id: str,
    *,
    spec: TriggerBindingSpec,
    actor: Actor,
    session: AsyncSession,
) -> UUID:
    """Validates: `intake_point_id` registered · schedule present iff the
    point's kind is `schedule` (cron parses, `notify_user_ids` non-empty and
    ⊆ org membership) · `pipeline_id` belongs to this org (FK alone can't
    check org — see module docstring). Audits `repo.trigger_added`."""
    points = {point.id: point for point in list_intake_points()}
    point = points.get(spec.intake_point_id)
    if point is None:
        raise UnknownIntakePointError(spec.intake_point_id)

    member_ids = set(await list_active_member_ids(session, org_id)) if point.kind == "schedule" else set()
    _validate_schedule(point.kind, spec.schedule, member_ids=member_ids)

    if _pipeline_lookup is None:
        raise RuntimeError("pipeline lookup not registered — domain.pipelines must be imported")
    pipeline_ref = await _pipeline_lookup(spec.pipeline_id, session)
    if pipeline_ref is None or pipeline_ref.org_id != org_id:
        raise UnknownPipelineError(spec.pipeline_id)

    if point.kind != "schedule":
        existing = (
            await session.execute(
                select(RepoTriggerBindingRow.id).where(
                    RepoTriggerBindingRow.org_id == org_id,
                    RepoTriggerBindingRow.repo_external_id == repo_external_id,
                    RepoTriggerBindingRow.intake_point_id == spec.intake_point_id,
                    RepoTriggerBindingRow.schedule.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise DuplicateBindingError(spec.intake_point_id)

    row = RepoTriggerBindingRow(
        org_id=org_id,
        repo_external_id=repo_external_id,
        intake_point_id=spec.intake_point_id,
        pipeline_id=spec.pipeline_id,
        schedule=spec.schedule.model_dump(mode="json") if spec.schedule is not None else None,
    )
    session.add(row)
    await session.flush()
    await audit(
        "repo_trigger_binding",
        row.id,
        "repo.trigger_added",
        _TriggerAddedPayload(
            repo_external_id=repo_external_id,
            intake_point_id=spec.intake_point_id,
            pipeline_id=spec.pipeline_id,
        ),
        actor=actor,
        org_id=org_id,
        session=session,
    )
    return row.id


async def remove_binding(binding_id: UUID, *, actor: Actor, session: AsyncSession) -> None:
    """Fetch + assert org via context; audits `repo.trigger_removed`."""
    org_id = require_org_context()
    row = await session.get(RepoTriggerBindingRow, binding_id)
    if row is None or row.org_id != org_id:
        raise BindingNotFoundError(binding_id)
    repo_external_id = row.repo_external_id
    intake_point_id = row.intake_point_id
    await session.delete(row)
    await session.flush()
    await audit(
        "repo_trigger_binding",
        binding_id,
        "repo.trigger_removed",
        _TriggerRemovedPayload(repo_external_id=repo_external_id, intake_point_id=intake_point_id),
        actor=actor,
        org_id=org_id,
        session=session,
    )


async def _to_trigger_binding(row: RepoTriggerBindingRow, *, session: AsyncSession) -> TriggerBinding:
    pipeline_ref = _pipeline_lookup and await _pipeline_lookup(row.pipeline_id, session)
    return TriggerBinding(
        id=row.id,
        repo_external_id=row.repo_external_id,
        intake_point_id=row.intake_point_id,
        pipeline_id=row.pipeline_id,
        pipeline_name=pipeline_ref.name if pipeline_ref is not None else "",
        schedule=Schedule.model_validate(row.schedule) if row.schedule is not None else None,
    )


async def find_bindings(
    org_id: UUID, repo_external_id: str, intake_point_id: str, *, session: AsyncSession
) -> list[TriggerBinding]:
    rows = (
        (
            await session.execute(
                select(RepoTriggerBindingRow).where(
                    RepoTriggerBindingRow.org_id == org_id,
                    RepoTriggerBindingRow.repo_external_id == repo_external_id,
                    RepoTriggerBindingRow.intake_point_id == intake_point_id,
                )
            )
        )
        .scalars()
        .all()
    )
    return [await _to_trigger_binding(row, session=session) for row in rows]


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
    """True iff any `repo_trigger_bindings` row targets `pipeline_id` — in
    any org (a pipeline's own org already scopes which bindings could
    reference it; the caller, `domain.pipelines.delete_pipeline`, has
    already asserted org ownership of `pipeline_id` itself)."""
    existing = (
        await session.execute(
            select(RepoTriggerBindingRow.id).where(RepoTriggerBindingRow.pipeline_id == pipeline_id).limit(1)
        )
    ).scalar_one_or_none()
    return existing is not None


async def list_due_schedule_bindings(*, now: datetime, session: AsyncSession) -> list[DueFire]:
    """Cron matching (UTC, floored-minute slot) over schedule bindings, across
    every org — the caller (`domain/pipelines.pipeline_schedule_tick`) has no
    single org to scope by; it's a global per-minute scan. Consumed by
    `domain/pipelines`' `pipeline_schedule_tick`."""
    slot = now.replace(second=0, microsecond=0)
    rows = (
        (
            await session.execute(
                select(RepoTriggerBindingRow).where(RepoTriggerBindingRow.schedule.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    due: list[DueFire] = []
    for row in rows:
        assert row.schedule is not None
        schedule = Schedule.model_validate(row.schedule)
        try:
            cron = CronExpr.parse(schedule.cron)
        except ValueError:
            # A malformed cron shouldn't happen (validated at write by
            # `_validate_schedule`), but a bad row must never crash the tick.
            continue
        if not cron.matches(slot):
            continue
        binding = await _to_trigger_binding(row, session=session)
        due.append(DueFire(org_id=row.org_id, binding=binding, fire_time=slot))
    return due
