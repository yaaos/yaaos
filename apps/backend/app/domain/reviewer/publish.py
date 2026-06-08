"""Convert `ReportedFinding`s from the coding-agent into persisted `Finding` rows.

`publish_findings` is the single entry point: open a `Review`, convert each
`ReportedFinding → Finding` (validating severity/confidence strings), assign a
per-PR `finding_display_id`, persist, and post each finding to the VCS plugin
via `vcs.post_finding`. No value object crosses the `vcs` boundary — findings
pass as named primitive args.
"""

from __future__ import annotations

import re
import uuid
from typing import get_args

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.coding_agent import ReportedFinding
from app.domain.reviewer.models import FindingRow, ReviewRow
from app.domain.reviewer.types import Confidence, Finding, Review, ReviewScope, Severity

log = structlog.get_logger("reviewer.publish")

# Category → short prefix for `finding_display_id` rendering.
_CATEGORY_PREFIX: dict[str, str] = {
    "security": "sec",
    "architecture": "arch",
    "performance": "perf",
    "correctness": "bug",
    "testing": "test",
    "documentation": "doc",
    "style": "style",
}

_VALID_SEVERITIES: frozenset[str] = frozenset(get_args(Severity))
_VALID_CONFIDENCES: frozenset[str] = frozenset(get_args(Confidence))


def category_prefix(category: str) -> str:
    """Derive the short prefix string for a category.

    Known categories map to fixed short codes. Unknown categories are
    slugified (lowercase alnum, max 8 chars).
    """
    key = category.lower().strip()
    if key in _CATEGORY_PREFIX:
        return _CATEGORY_PREFIX[key]
    slug = re.sub(r"[^a-z0-9]", "", key)[:8] or "find"
    return slug


def finding_handle(category: str, finding_display_id: int) -> str:
    """Render the user-visible finding handle, e.g. `sec-1`."""
    return f"{category_prefix(category)}-{finding_display_id}"


def _validate_severity(raw: str) -> Severity:  # type: ignore[return]
    if raw not in _VALID_SEVERITIES:
        raise ValueError(f"invalid severity {raw!r}; must be one of {sorted(_VALID_SEVERITIES)}")
    return raw  # type: ignore[return-value]


def _validate_confidence(raw: str) -> Confidence:  # type: ignore[return]
    if raw not in _VALID_CONFIDENCES:
        raise ValueError(f"invalid confidence {raw!r}; must be one of {sorted(_VALID_CONFIDENCES)}")
    return raw  # type: ignore[return-value]


async def _next_finding_display_id(pr_id: uuid.UUID, session: AsyncSession) -> int:
    """Return `max(finding_display_id) + 1` for findings within `pr_id`.

    Runs inside the caller's transaction under the advisory lock held by
    `acquire_pr_lock`; concurrent insertions serialize there.
    """
    result = await session.execute(
        select(func.max(FindingRow.finding_display_id)).where(FindingRow.pr_id == pr_id)
    )
    current_max = result.scalar_one_or_none()
    return (current_max or 0) + 1


async def publish_findings(
    *,
    pr_id: uuid.UUID,
    org_id: uuid.UUID,
    pr_external_id: str,
    vcs_plugin_id: str,
    findings: list[ReportedFinding],
    run_id: uuid.UUID | None = None,
    session: AsyncSession,
) -> tuple[Review, list[Finding]]:
    """Convert + persist `ReportedFinding`s and post them to the VCS plugin.

    Opens a new `Review` row, converts each `ReportedFinding` to a `Finding`
    (validating severity/confidence — out-of-range raises `ValueError`),
    assigns a per-PR monotonic `finding_display_id`, persists, and posts
    each finding via `vcs.post_finding` with named primitive args.

    Caller holds the per-PR advisory lock (`acquire_pr_lock`). Caller commits.

    Returns `(Review, list[Finding])` — the review and the admitted findings.
    """
    from app.domain.reviewer.lock import acquire_pr_lock  # noqa: PLC0415

    await acquire_pr_lock(session, pr_id)

    # ── Open review row ───────────────────────────────────────────────────
    # Sequence number = max existing + 1 for this PR.
    seq_result = await session.execute(select(func.count(ReviewRow.id)).where(ReviewRow.pr_id == pr_id))
    sequence_number = (seq_result.scalar_one() or 0) + 1

    review_id = uuid.uuid7()
    review_row = ReviewRow(
        id=review_id,
        org_id=org_id,
        pr_id=pr_id,
        sequence_number=sequence_number,
        status="running",
        trigger_reason="pr_ready",
        scope_kind="full",
        run_id=run_id,
    )
    session.add(review_row)
    await session.flush()

    # ── Convert + validate each finding ──────────────────────────────────
    admitted: list[Finding] = []
    finding_rows: list[FindingRow] = []
    next_display_id = await _next_finding_display_id(pr_id, session)

    for rf in findings:
        severity = _validate_severity(rf.severity)
        confidence = _validate_confidence(rf.confidence)
        display_id = next_display_id
        next_display_id += 1

        from datetime import UTC, datetime  # noqa: PLC0415

        now = datetime.now(UTC)
        f = Finding(
            id=uuid.uuid7(),
            pr_id=pr_id,
            org_id=org_id,
            review_id=review_id,
            finding_display_id=display_id,
            category=rf.category.lower().strip(),
            severity=severity,
            confidence=confidence,
            rationale=rf.rationale,
            rule_violated=rf.rule_violated,
            rule_source=rf.rule_source,
            suggested_fix=rf.suggested_fix,
            file=rf.file,
            line=rf.line,
            created_at=now,
            updated_at=now,
        )
        row = FindingRow(
            id=f.id,
            org_id=org_id,
            pr_id=pr_id,
            review_id=review_id,
            finding_display_id=display_id,
            category=f.category,
            severity=str(severity),
            confidence=str(confidence),
            rationale=rf.rationale,
            rule_violated=rf.rule_violated,
            rule_source=rf.rule_source,
            suggested_fix=rf.suggested_fix,
            file=rf.file,
            line=rf.line,
        )
        session.add(row)
        admitted.append(f)
        finding_rows.append(row)

    # ── Post to VCS via post_finding ──────────────────────────────────────
    if admitted:
        await _post_findings_via_vcs(
            pr_external_id=pr_external_id,
            vcs_plugin_id=vcs_plugin_id,
            findings=admitted,
        )

    # ── Mark review done ──────────────────────────────────────────────────
    review_row.status = "done"
    await session.flush()

    log.info(
        "publish_findings.done",
        pr_id=str(pr_id),
        org_id=str(org_id),
        review_id=str(review_id),
        admitted=len(admitted),
    )

    from datetime import UTC, datetime  # noqa: PLC0415

    now = datetime.now(UTC)
    review = Review(
        id=review_id,
        pr_id=pr_id,
        org_id=org_id,
        sequence_number=sequence_number,
        trigger_reason="pr_ready",
        scope=ReviewScope.full("", ""),
        commit_sha_at_start="",
        status="done",
        created_at=now,
    )
    return review, admitted


async def _post_findings_via_vcs(
    *,
    pr_external_id: str,
    vcs_plugin_id: str,
    findings: list[Finding],
) -> None:
    """Post each finding to the VCS plugin via `post_finding`.

    Passes named primitive args — no value object crosses the `vcs` boundary.
    Each finding is posted independently; the plugin renders per-platform.
    """
    from app.domain.vcs import get_plugin as get_vcs_plugin  # noqa: PLC0415

    plugin = get_vcs_plugin(vcs_plugin_id)

    for f in findings:
        try:
            await plugin.post_finding(
                pr_external_id,
                file=f.file,
                line_start=f.line,
                line_end=f.line,
                severity=f.severity,
                category=f.category,
                confidence=f.confidence,
                finding_display_id=f.finding_display_id,
                rationale=f.rationale,
                rule_violated=f.rule_violated,
                rule_source=f.rule_source,
                suggested_fix=f.suggested_fix,
            )
        except Exception:
            log.exception(
                "publish_findings.vcs_post_failed",
                pr_external_id=pr_external_id,
                finding_display_id=f.finding_display_id,
            )
            raise


__all__ = [
    "category_prefix",
    "finding_handle",
    "publish_findings",
]
