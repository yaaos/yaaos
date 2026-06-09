"""Service layer for the reviewer's read and event-dispatch operations.

Provides:
- `list_findings_for_pr` — read-side list of findings for a PR.
- `list_reviews_for_pr` / `get_review` — read-side review history.
- `aggregate_findings_by_prs` — batch rollup (count + max severity) for the
  ticket list view.
- `refresh_ticket_findings_summary` — updates the ticket row's rollup after
  findings change.
- `dispatch_events` — queues reviewer domain events to the SSE bus.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_org_context
from app.core.sse import GeneralEventKind, publish_general_after_commit
from app.domain.reviewer.types import Finding, Review

_YAAOS_COMMAND_RE = re.compile(r"@yaaos\s+(review|full\s+review|cancel)\b", re.IGNORECASE)
_FIX_CLAIM_RE = re.compile(r"\b(fix(ed|ing)?|done|address(ed|ing)?|resolved)\b", re.IGNORECASE)


def is_yaaos_command(body: str) -> str | None:
    """Returns the command name (`review` | `full review` | `cancel`) or None."""
    m = _YAAOS_COMMAND_RE.search(body)
    return m.group(1).lower().replace("  ", " ") if m else None


def is_off_topic_message(body: str) -> bool:
    """Heuristic: short, no question, no fix claim → don't classify."""
    stripped = body.strip()
    if "?" in stripped:
        return False
    if _FIX_CLAIM_RE.search(stripped):
        return False
    return len(stripped.split()) < 5


# ─── Read views ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FindingView:
    """Read-model row consumed by the ticket findings UI."""

    id: uuid.UUID
    finding_display_id: int
    category: str
    severity: str
    confidence: str
    rationale: str
    rule_violated: str
    rule_source: str
    suggested_fix: str
    file: str | None
    line: int | None
    review_id: uuid.UUID


def _finding_view(f: Finding) -> FindingView:
    return FindingView(
        id=f.id,
        finding_display_id=f.finding_display_id,
        category=f.category,
        severity=f.severity,
        confidence=f.confidence,
        rationale=f.rationale,
        rule_violated=f.rule_violated,
        rule_source=f.rule_source,
        suggested_fix=f.suggested_fix,
        file=f.file,
        line=f.line,
        review_id=f.review_id,
    )


# ─── Public Python API ──────────────────────────────────────────────────────


async def list_reviews_for_pr(pr_id: uuid.UUID, *, org_id: uuid.UUID) -> list[Review]:
    """List Review entities for a PR, newest first."""
    from sqlalchemy import desc, select  # noqa: PLC0415

    from app.core.database import session as db_session  # noqa: PLC0415
    from app.domain.reviewer.models import ReviewRow  # noqa: PLC0415
    from app.domain.reviewer.repository import _review_from_row  # noqa: PLC0415

    async with db_session() as s:
        rows = (
            (
                await s.execute(
                    select(ReviewRow)
                    .where(ReviewRow.pr_id == pr_id, ReviewRow.org_id == org_id)
                    .order_by(desc(ReviewRow.sequence_number))
                )
            )
            .scalars()
            .all()
        )
    return [_review_from_row(r) for r in rows]


async def get_review(review_id: uuid.UUID, *, org_id: uuid.UUID) -> Review:
    """Fetch one Review by id. Raises `LookupError` if missing."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.core.database import session as db_session  # noqa: PLC0415
    from app.domain.reviewer.models import ReviewRow  # noqa: PLC0415
    from app.domain.reviewer.repository import _review_from_row  # noqa: PLC0415

    async with db_session() as s:
        row = (
            await s.execute(select(ReviewRow).where(ReviewRow.id == review_id, ReviewRow.org_id == org_id))
        ).scalar_one_or_none()
    if row is None:
        raise LookupError(f"review {review_id} not found in org {org_id}")
    return _review_from_row(row)


async def list_findings_for_pr(
    pr_id: uuid.UUID, *, org_id: uuid.UUID, include_terminal: bool = False
) -> list[FindingView]:
    """List findings for a PR, most recent first."""
    from sqlalchemy import desc, select  # noqa: PLC0415

    from app.core.database import session as db_session  # noqa: PLC0415
    from app.domain.reviewer.models import FindingRow  # noqa: PLC0415
    from app.domain.reviewer.repository import _finding_from_row  # noqa: PLC0415

    del include_terminal  # all findings are terminal in the canonical schema

    async with db_session() as s:
        rows = (
            (
                await s.execute(
                    select(FindingRow)
                    .where(FindingRow.pr_id == pr_id, FindingRow.org_id == org_id)
                    .order_by(desc(FindingRow.finding_display_id))
                )
            )
            .scalars()
            .all()
        )
    return [_finding_view(_finding_from_row(r)) for r in rows]


# ─── Eval metrics ────────────────────────────────────────────────────────────


async def aggregate_findings_by_prs(
    pr_ids: list[uuid.UUID], *, org_id: uuid.UUID
) -> dict[uuid.UUID, tuple[int, str | None]]:
    """Return finding count and max severity for each pr_id in one query.

    Keys are present only for pr_ids that have at least one finding.
    Value is `(count, max_severity)` where max_severity is the highest
    severity value in `{blocker, should_fix, nit}` ordering.
    """
    from sqlalchemy import case, func, select  # noqa: PLC0415

    from app.core.database import session as db_session  # noqa: PLC0415
    from app.domain.reviewer.models import FindingRow  # noqa: PLC0415

    if not pr_ids:
        return {}

    severity_rank = case(
        (FindingRow.severity == "blocker", 3),
        (FindingRow.severity == "should_fix", 2),
        (FindingRow.severity == "nit", 1),
        else_=0,
    )
    agg_stmt = (
        select(
            FindingRow.pr_id,
            func.count(FindingRow.id),
            func.max(severity_rank),
        )
        .where(FindingRow.pr_id.in_(pr_ids), FindingRow.org_id == org_id)
        .group_by(FindingRow.pr_id)
    )
    async with db_session() as s:
        results = (await s.execute(agg_stmt)).all()

    out: dict[uuid.UUID, tuple[int, str | None]] = {}
    for pr_id, count, max_rank in results:
        severity = {3: "blocker", 2: "should_fix", 1: "nit"}.get(int(max_rank or 0))
        out[pr_id] = (int(count), severity)
    return out


async def refresh_ticket_findings_summary(
    ticket_id: uuid.UUID,
    pr_id: uuid.UUID,
    *,
    org_id: uuid.UUID,
    session: Any,
) -> None:
    """Recompute findings rollup for `pr_id` and write it to the ticket row.

    Runs inside the caller's session; caller commits.
    """
    from app.domain.tickets import update_findings_summary  # noqa: PLC0415

    rollup = await aggregate_findings_by_prs([pr_id], org_id=org_id)
    count, severity = rollup.get(pr_id, (0, None))
    await update_findings_summary(
        ticket_id,
        findings_count=count,
        max_severity=severity,
        session=session,
    )


# ─── Domain events dispatch ────────────────────────────────────────────────

_KIND_MAP: dict[str, GeneralEventKind] = {
    "ReviewRequested": GeneralEventKind.REVIEW_REQUESTED,
    "ReviewStarted": GeneralEventKind.REVIEW_STARTED,
    "ReviewCompleted": GeneralEventKind.REVIEW_COMPLETED,
    "ReviewFailed": GeneralEventKind.REVIEW_FAILED,
    "FindingRaised": GeneralEventKind.FINDING_RAISED,
}


def dispatch_events(session: AsyncSession, *, events: list[Any]) -> list[Any]:
    """Queue reviewer domain events for publish after the session commits.

    Uses `publish_general_after_commit` — events are stashed on the SQLAlchemy
    session and flushed to Redis only after a successful `await session.commit()`.
    Returns the list of events queued (for tests / audit).
    """
    import dataclasses  # noqa: PLC0415
    import enum  # noqa: PLC0415

    org_id = require_org_context()
    for event in events:
        cls_name = type(event).__name__
        kind = _KIND_MAP.get(cls_name)
        if kind is None:
            continue

        raw = dataclasses.asdict(event)

        def _safe(v: Any) -> Any:
            if isinstance(v, uuid.UUID):
                return str(v)
            if isinstance(v, enum.Enum):
                return v.value
            if isinstance(v, dict):
                return {k: _safe(x) for k, x in v.items()}
            if isinstance(v, (list, tuple)):
                return [_safe(x) for x in v]
            return v

        payload = _safe(raw)
        publish_general_after_commit(session, org_id=org_id, kind=kind, payload=payload)
    return events
