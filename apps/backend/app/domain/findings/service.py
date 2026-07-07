"""Service surface for `domain/findings`.

`record_findings` is the only write path onto `pipeline_findings` — the
engine calls it once per review return (main-loop or standalone review
stage), for every reported finding, idempotent on id. The four transition
functions (`resolve`/`reopen`/`dismiss`/`reflag`) apply the matrix in
`apps/backend/docs/domain_findings.md`; each is idempotent on the current
status (no duplicate event) and raises `InvalidFindingTransition` on an
illegal jump. Every transition (plus `mark_defended`) writes an audit row.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, audit_for_finding
from app.domain.findings.models import FindingRow
from app.domain.findings.types import (
    AutoApproveConditions,
    Finding,
    FindingSpec,
    FindingStatusEvent,
    InvalidFindingTransition,
)
from app.domain.tickets import update_findings_summary

# `open -> {resolved, dismissed}`; `resolved -> {open, dismissed}`; `dismissed`
# is terminal. A re-sighting after dismissal is a NEW finding, never a
# transition back onto the dismissed row.
_LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"resolved", "dismissed"}),
    "resolved": frozenset({"open", "dismissed"}),
    "dismissed": frozenset(),
}

_SEVERITY_RANK = case(
    (FindingRow.severity == "blocker", 3),
    (FindingRow.severity == "should_fix", 2),
    (FindingRow.severity == "nit", 1),
    else_=0,
)
_RANK_TO_SEVERITY = {3: "blocker", 2: "should_fix", 1: "nit"}


class FindingNotFoundError(LookupError):
    """No finding row for the given id."""


class _DefendedPayload(BaseModel):
    defended_at: datetime


async def _get_row(finding_id: UUID, *, session: AsyncSession) -> FindingRow:
    row = await session.get(FindingRow, finding_id)
    if row is None:
        raise FindingNotFoundError(str(finding_id))
    return row


async def _append_event_and_audit(
    row: FindingRow, *, kind: str, event: FindingStatusEvent, session: AsyncSession
) -> None:
    row.status_events = [*row.status_events, event.model_dump(mode="json")]
    await session.flush()
    await audit_for_finding(row.id, kind, event, actor=event.actor, org_id=row.org_id, session=session)


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
    refreshes body/code_line/artifact_section/defect_in_artifact (latest
    wins), never duplicates; severity is immutable after creation.

    `display_id` is per-ticket monotonic (current max+1, computed once for
    the whole batch) — safe because one-run-per-ticket serializes writers,
    same convention as `domain/artifacts.store`'s version numbering.
    """
    if not findings:
        return []

    current_max = (
        await session.execute(
            select(func.max(FindingRow.display_id)).where(FindingRow.ticket_id == ticket_id)
        )
    ).scalar_one()
    next_display_id = (current_max or 0) + 1

    results: list[Finding] = []
    for spec in findings:
        existing = await session.get(FindingRow, spec.id)
        if existing is not None:
            existing.body = spec.body
            existing.code_file = spec.code_file
            existing.code_line = spec.code_line
            existing.artifact_section = spec.artifact_section
            existing.defect_in_artifact = spec.defect_in_artifact
            await session.flush()
            # `updated_at` (onupdate=func.now()) is expired by the flush;
            # refresh explicitly so `Finding.from_row`'s plain attribute
            # access below doesn't trigger a lazy DB round-trip outside the
            # async greenlet context.
            await session.refresh(existing)
            results.append(Finding.from_row(existing))
            continue

        row = FindingRow(
            id=spec.id,
            org_id=org_id,
            ticket_id=ticket_id,
            source_run_id=run_id,
            source_stage_name=stage_name,
            source_stage_execution_id=stage_execution_id,
            first_seen_iteration=iteration,
            display_prefix=spec.display_prefix,
            display_id=next_display_id,
            severity=spec.severity,
            body=spec.body,
            code_file=spec.code_file,
            code_line=spec.code_line,
            artifact_section=spec.artifact_section,
            defect_in_artifact=spec.defect_in_artifact,
            status="open",
            status_events=[],
        )
        next_display_id += 1
        session.add(row)
        await session.flush()
        results.append(Finding.from_row(row))
    return results


async def get(finding_id: UUID, *, session: AsyncSession) -> Finding:
    """Fetch one finding by id. Raises `FindingNotFoundError`."""
    return Finding.from_row(await _get_row(finding_id, session=session))


async def set_external_anchor(finding_id: UUID, *, comment_external_id: str, session: AsyncSession) -> None:
    """Stamp the PR comment id a finding was posted under. Idempotent — a
    posting action calls this once per finding, after either a fresh
    `vcs.post_finding` or reconciling an already-posted comment discovered
    via `vcs.list_yaaos_comments`; re-stamping the same id is a harmless
    overwrite. Metadata only — no status transition, no audit row."""
    row = await _get_row(finding_id, session=session)
    row.external_comment_id = comment_external_id
    await session.flush()


async def resolve(finding_id: UUID, *, event: FindingStatusEvent, session: AsyncSession) -> None:
    row = await _get_row(finding_id, session=session)
    if row.status == "resolved":
        return
    if "resolved" not in _LEGAL_TRANSITIONS.get(row.status, frozenset()):
        raise InvalidFindingTransition(f"cannot resolve finding {finding_id} from status {row.status!r}")
    row.status = "resolved"
    await _append_event_and_audit(row, kind="finding.resolved", event=event, session=session)


async def reopen(finding_id: UUID, *, event: FindingStatusEvent, session: AsyncSession) -> None:
    row = await _get_row(finding_id, session=session)
    if row.status == "open":
        return
    if "open" not in _LEGAL_TRANSITIONS.get(row.status, frozenset()):
        raise InvalidFindingTransition(f"cannot reopen finding {finding_id} from status {row.status!r}")
    row.status = "open"
    await _append_event_and_audit(row, kind="finding.reopened", event=event, session=session)


async def dismiss(finding_id: UUID, *, event: FindingStatusEvent, session: AsyncSession) -> None:
    row = await _get_row(finding_id, session=session)
    if row.status == "dismissed":
        return
    if "dismissed" not in _LEGAL_TRANSITIONS.get(row.status, frozenset()):
        raise InvalidFindingTransition(f"cannot dismiss finding {finding_id} from status {row.status!r}")
    row.status = "dismissed"
    await _append_event_and_audit(row, kind="finding.dismissed", event=event, session=session)


async def reflag(finding_id: UUID, *, event: FindingStatusEvent, session: AsyncSession) -> None:
    """Re-assertion: appends an event with status unchanged ("fix claim
    verified false", "sighted again"). Only legal from `open` — a resolved
    finding re-sighted goes through `reopen`, not `reflag`."""
    row = await _get_row(finding_id, session=session)
    if row.status != "open":
        raise InvalidFindingTransition(
            f"cannot reflag finding {finding_id} from status {row.status!r} — only an open finding reflags"
        )
    await _append_event_and_audit(row, kind="finding.reflagged", event=event, session=session)


async def mark_defended(finding_id: UUID, *, session: AsyncSession) -> None:
    """Stamp `defended_at` once (idempotent — a second call is a no-op)."""
    row = await _get_row(finding_id, session=session)
    if row.defended_at is not None:
        return
    now = datetime.now(UTC)
    row.defended_at = now
    await session.flush()
    await audit_for_finding(
        row.id,
        "finding.defended",
        _DefendedPayload(defended_at=now),
        actor=Actor.system(),
        org_id=row.org_id,
        session=session,
    )


async def list_open_for_ticket(org_id: UUID, ticket_id: UUID, *, session: AsyncSession) -> list[Finding]:
    rows = (
        (
            await session.execute(
                select(FindingRow)
                .where(
                    FindingRow.org_id == org_id,
                    FindingRow.ticket_id == ticket_id,
                    FindingRow.status == "open",
                )
                .order_by(FindingRow.display_id)
            )
        )
        .scalars()
        .all()
    )
    return [Finding.from_row(row) for row in rows]


async def list_for_stage_execution(stage_execution_id: UUID, *, session: AsyncSession) -> list[Finding]:
    """Residual computation: this stage execution's own findings, any status."""
    rows = (
        (
            await session.execute(
                select(FindingRow)
                .where(FindingRow.source_stage_execution_id == stage_execution_id)
                .order_by(FindingRow.display_id)
            )
        )
        .scalars()
        .all()
    )
    return [Finding.from_row(row) for row in rows]


async def find_by_external_comment(
    org_id: UUID, comment_external_id: str, *, session: AsyncSession
) -> Finding | None:
    row = (
        await session.execute(
            select(FindingRow).where(
                FindingRow.org_id == org_id, FindingRow.external_comment_id == comment_external_id
            )
        )
    ).scalar_one_or_none()
    return Finding.from_row(row) if row is not None else None


async def evaluate_auto_approve(
    org_id: UUID, ticket_id: UUID, *, conditions: AutoApproveConditions, session: AsyncSession
) -> bool:
    """Scope: findings posted to the PR (external_comment_id set)."""
    raise NotImplementedError


async def refresh_ticket_summary(org_id: UUID, ticket_id: UUID, *, session: AsyncSession) -> None:
    """Feed `tickets.findings_count` (open count) / `tickets.max_severity`."""
    count, max_rank = (
        await session.execute(
            select(func.count(FindingRow.id), func.max(_SEVERITY_RANK)).where(
                FindingRow.org_id == org_id,
                FindingRow.ticket_id == ticket_id,
                FindingRow.status == "open",
            )
        )
    ).one()
    severity = _RANK_TO_SEVERITY.get(int(max_rank or 0))
    await update_findings_summary(
        ticket_id, findings_count=int(count), max_severity=severity, session=session
    )
