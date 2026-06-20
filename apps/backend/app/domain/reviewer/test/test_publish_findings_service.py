"""Service tests for `publish_findings` — the canonical finding pipeline.

Covers the behaviors the publish path guarantees:
1. `finding_display_id` assignment is per-`pr_id` monotonic and unique under
   concurrent inserts.
2. Each `ReportedFindingShape` results in one `vcs.post_finding` call; null-anchor
   findings (no file/line) still produce a `post_finding` call.
3. The `ReportedFindingShape` field set is pinned to `CodeReviewResponse` schema —
   the single source of truth for the agent's response contract.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import get_args

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.core.vcs import VCSPullRequest
from app.domain.reviewer import CodeReviewResponse, ReportedFindingShape, publish_findings
from app.domain.reviewer.models import FindingRow
from app.domain.reviewer.types import Confidence, Severity
from app.domain.tickets import create_from_pr as create_ticket
from app.domain.tickets import upsert as upsert_pr
from app.testing.stub_vcs import register_stub_vcs


def _conforming(category: str = "security", severity: str = "blocker") -> ReportedFindingShape:
    return ReportedFindingShape(
        file="src/foo.py",
        line=10,
        category=category,
        severity=severity,  # type: ignore[arg-type]
        confidence="verified",
        rationale="reason",
        rule_violated="rule-x",
        rule_source="house",
        suggested_fix="do thing",
    )


async def _seed_pr(db_session) -> tuple[uuid.UUID, uuid.UUID, str, str]:  # type: ignore[no-untyped-def]
    """Seed an org + ticket + PR row so findings can FK to a real `pr_id`.

    Returns `(org_id, pr_id, pr_external_id, vcs_plugin_id)`.
    """
    org_id = uuid.uuid4()
    ext_id = f"42-{uuid.uuid4().hex[:6]}"
    ticket_id, _created = await create_ticket(
        org_id=org_id,
        source_external_id=ext_id,
        title="t",
        description=None,
        repo_external_id="me/repo",
        plugin_id="github",
        idempotency_key=ext_id,
        payload={"head_sha": "deadbeef"},
        session=db_session,
    )
    pr = await upsert_pr(
        VCSPullRequest(
            plugin_id="github",
            repo_external_id="me/repo",
            external_id=f"pr-{ext_id}",
            number=42,
            title="t",
            body=None,
            author_login="alice",
            author_type="user",
            base_branch="main",
            head_branch="feature",
            base_sha="babecafe",
            head_sha="deadbeef",
            is_draft=False,
            is_fork=False,
            state="open",
            html_url="http://test",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        ),
        ticket_id=ticket_id,
        org_id=org_id,
        session=db_session,
    )
    await db_session.commit()
    return org_id, pr.id, f"pr-{ext_id}", "github"


# ── Schema strictness (at construction) ────────────────────────────────────


def test_reported_finding_shape_rejects_invalid_severity() -> None:
    """ReportedFindingShape raises ValidationError on an out-of-range severity.

    The strict Literal type enforces the contract at Pydantic construction
    time — before any DB call. `publish_findings` callers always receive
    fully-validated shapes from `handle_response`.
    """
    with pytest.raises(ValidationError, match="severity"):
        ReportedFindingShape(
            category="security",
            severity="major",  # type: ignore[arg-type]  — not a valid Literal
            confidence="verified",
            rationale="r",
            rule_violated="r",
            rule_source="s",
            suggested_fix="f",
        )


def test_reported_finding_shape_rejects_invalid_confidence() -> None:
    """ReportedFindingShape raises ValidationError on an out-of-range confidence."""
    with pytest.raises(ValidationError, match="confidence"):
        ReportedFindingShape(
            category="security",
            severity="blocker",
            confidence="totally-sure",  # type: ignore[arg-type]
            rationale="r",
            rule_violated="r",
            rule_source="s",
            suggested_fix="f",
        )


@pytest.mark.service
@pytest.mark.asyncio
async def test_publish_findings_persists_conforming_input(db_session) -> None:  # type: ignore[no-untyped-def]
    org_id, pr_id, pr_external_id, vcs_plugin_id = await _seed_pr(db_session)

    findings = [_conforming(category="security"), _conforming(category="performance", severity="nit")]

    with register_stub_vcs(plugin_id="github"):
        await publish_findings(
            pr_id=pr_id,
            org_id=org_id,
            pr_external_id=pr_external_id,
            vcs_plugin_id=vcs_plugin_id,
            findings=findings,
            session=db_session,
        )

    rows = (await db_session.execute(select(FindingRow).where(FindingRow.pr_id == pr_id))).scalars().all()
    assert len(rows) == 2
    severities = {r.severity for r in rows}
    assert severities == {"blocker", "nit"}, severities


# ── finding_display_id monotonicity ────────────────────────────────────────


@pytest.mark.service
@pytest.mark.asyncio
async def test_finding_display_id_is_monotonic_per_pr(db_session) -> None:  # type: ignore[no-untyped-def]
    """First publish gets ids 1..N; second publish on the same PR continues
    from N+1. Ids are per-`pr_id`, not global.
    """
    org_id, pr_id, pr_external_id, vcs_plugin_id = await _seed_pr(db_session)

    with register_stub_vcs(plugin_id="github"):
        await publish_findings(
            pr_id=pr_id,
            org_id=org_id,
            pr_external_id=pr_external_id,
            vcs_plugin_id=vcs_plugin_id,
            findings=[_conforming(), _conforming(category="performance")],
            session=db_session,
        )
        await publish_findings(
            pr_id=pr_id,
            org_id=org_id,
            pr_external_id=pr_external_id,
            vcs_plugin_id=vcs_plugin_id,
            findings=[_conforming(category="testing", severity="nit")],
            session=db_session,
        )

    ids = sorted(
        (await db_session.execute(select(FindingRow.finding_display_id).where(FindingRow.pr_id == pr_id)))
        .scalars()
        .all()
    )
    assert ids == [1, 2, 3], ids


@pytest.mark.service
@pytest.mark.asyncio
async def test_finding_display_id_unique_across_multi_finding_publish(db_session) -> None:  # type: ignore[no-untyped-def]
    """Multiple findings within a single publish call land on distinct
    `finding_display_id` values.
    """
    org_id, pr_id, pr_external_id, vcs_plugin_id = await _seed_pr(db_session)

    findings = [
        _conforming(category="security"),
        _conforming(category="performance"),
        _conforming(category="testing", severity="nit"),
    ]
    with register_stub_vcs(plugin_id="github"):
        await publish_findings(
            pr_id=pr_id,
            org_id=org_id,
            pr_external_id=pr_external_id,
            vcs_plugin_id=vcs_plugin_id,
            findings=findings,
            session=db_session,
        )

    ids = sorted(
        (await db_session.execute(select(FindingRow.finding_display_id).where(FindingRow.pr_id == pr_id)))
        .scalars()
        .all()
    )
    assert len(ids) == len(set(ids)), f"duplicate ids: {ids}"
    assert ids == [1, 2, 3], ids


# ── Schema pin ──────────────────────────────────────────────────────────────


def test_reported_finding_shape_field_set_matches_code_review_response_schema() -> None:
    """`ReportedFindingShape`'s field set matches the `CodeReviewResponse` JSON schema's
    `findings[*]` item properties. This pin catches drift between the two.
    """
    schema = CodeReviewResponse.model_json_schema()
    # CodeReviewResponse shape: {"findings": [{<item>}]}
    item_ref = schema["properties"]["findings"]["items"]
    ref_key = item_ref["$ref"].rsplit("/", 1)[-1]
    item_schema = schema["$defs"][ref_key]
    schema_fields = set(item_schema["properties"].keys())

    shape_fields = set(ReportedFindingShape.model_fields.keys())
    assert shape_fields == schema_fields, (
        f"drift: ReportedFindingShape has {shape_fields - schema_fields}, "
        f"schema has {schema_fields - shape_fields}"
    )


def test_reported_finding_shape_severity_and_confidence_match_typed_aliases() -> None:
    """The enum values in `CodeReviewResponse` schema must equal the `Severity`/`Confidence`
    Literal tuples — any drift breaks publish at runtime.
    """
    schema = CodeReviewResponse.model_json_schema()
    item_ref = schema["properties"]["findings"]["items"]
    ref_key = item_ref["$ref"].rsplit("/", 1)[-1]
    item_schema = schema["$defs"][ref_key]

    schema_sev = set(item_schema["properties"]["severity"]["enum"])
    schema_conf = set(item_schema["properties"]["confidence"]["enum"])
    assert schema_sev == set(get_args(Severity)), (
        f"severity: schema {schema_sev} vs typed {get_args(Severity)}"
    )
    assert schema_conf == set(get_args(Confidence)), (
        f"confidence: schema {schema_conf} vs typed {get_args(Confidence)}"
    )


# ── VCS post_finding transport ─────────────────────────────────────────────


@pytest.mark.service
@pytest.mark.asyncio
async def test_publish_findings_calls_post_finding_once_per_finding(db_session) -> None:  # type: ignore[no-untyped-def]
    """Each `ReportedFindingShape` maps to exactly one `vcs.post_finding` call."""
    org_id, pr_id, pr_external_id, vcs_plugin_id = await _seed_pr(db_session)

    findings = [
        _conforming(category="security", severity="blocker"),
        _conforming(category="correctness", severity="should_fix"),
    ]

    with register_stub_vcs(plugin_id="github") as stub:
        await publish_findings(
            pr_id=pr_id,
            org_id=org_id,
            pr_external_id=pr_external_id,
            vcs_plugin_id=vcs_plugin_id,
            findings=findings,
            session=db_session,
        )

    assert len(stub.posted_findings) == 2


@pytest.mark.service
@pytest.mark.asyncio
async def test_publish_findings_null_anchor_calls_post_finding(db_session) -> None:  # type: ignore[no-untyped-def]
    """A finding with no `file`/`line` (null anchor) still calls `post_finding`."""
    org_id, pr_id, pr_external_id, vcs_plugin_id = await _seed_pr(db_session)

    null_anchor = ReportedFindingShape(
        file=None,
        line=None,
        category="architecture",
        severity="nit",
        confidence="speculative",
        rationale="PR-wide observation.",
        rule_violated="no-rule",
        rule_source="house",
        suggested_fix="",
    )

    with register_stub_vcs(plugin_id="github") as stub:
        await publish_findings(
            pr_id=pr_id,
            org_id=org_id,
            pr_external_id=pr_external_id,
            vcs_plugin_id=vcs_plugin_id,
            findings=[null_anchor],
            session=db_session,
        )

    assert len(stub.posted_findings) == 1
    _org_id, _ext_id, kwargs = stub.posted_findings[0]
    assert kwargs["file"] is None
    assert kwargs["line_start"] is None
