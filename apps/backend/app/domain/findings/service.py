"""Stub service surface for `domain/findings`.

Bodies raise `NotImplementedError` — only the signatures are load-bearing.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.findings.types import AutoApproveConditions, Finding, FindingSpec, FindingStatusEvent


class FindingNotFoundError(LookupError):
    """No finding row for the given id."""


async def record_findings(
    *,
    org_id: UUID,
    ticket_id: UUID,
    run_id: UUID,
    stage_name: str,
    stage_execution_id: UUID,
    iteration: int,
    findings: Sequence[FindingSpec],
    session: AsyncSession,
) -> list[Finding]:
    """Materialize every reported finding. Idempotent on id: a re-report
    refreshes body/code_line, never duplicates; severity is immutable."""
    raise NotImplementedError


async def set_external_anchor(finding_id: UUID, *, comment_external_id: str, session: AsyncSession) -> None:
    """Stamp the PR comment id a finding was posted under."""
    raise NotImplementedError


async def resolve(finding_id: UUID, *, event: FindingStatusEvent, session: AsyncSession) -> None:
    raise NotImplementedError


async def reopen(finding_id: UUID, *, event: FindingStatusEvent, session: AsyncSession) -> None:
    raise NotImplementedError


async def dismiss(finding_id: UUID, *, event: FindingStatusEvent, session: AsyncSession) -> None:
    raise NotImplementedError


async def reflag(finding_id: UUID, *, event: FindingStatusEvent, session: AsyncSession) -> None:
    """Re-assertion: appends an event with status unchanged."""
    raise NotImplementedError


async def mark_defended(finding_id: UUID, *, session: AsyncSession) -> None:
    """Stamp `defended_at` once."""
    raise NotImplementedError


async def list_open_for_ticket(org_id: UUID, ticket_id: UUID, *, session: AsyncSession) -> list[Finding]:
    raise NotImplementedError


async def list_for_stage_execution(stage_execution_id: UUID, *, session: AsyncSession) -> list[Finding]:
    raise NotImplementedError


async def find_by_external_comment(
    org_id: UUID, comment_external_id: str, *, session: AsyncSession
) -> Finding | None:
    raise NotImplementedError


async def evaluate_auto_approve(
    org_id: UUID, ticket_id: UUID, *, conditions: AutoApproveConditions, session: AsyncSession
) -> bool:
    """Scope: findings posted to the PR (external_comment_id set)."""
    raise NotImplementedError


async def refresh_ticket_summary(org_id: UUID, ticket_id: UUID, *, session: AsyncSession) -> None:
    """Feed `tickets.findings_count` / `tickets.max_severity`."""
    raise NotImplementedError
