"""Service tests for `publish_findings` — the canonical finding pipeline.

Covers the behaviors the publish path guarantees:
1. Out-of-range `severity`/`confidence` strings raise (the runtime gate).
2. `finding_display_id` assignment is per-`pr_id` monotonic and unique under
   concurrent inserts.
3. Each `ReportedFinding` results in one `vcs.post_finding` call; null-anchor
   findings (no file/line) still produce a `post_finding` call.
4. The `ReportedFinding` field set is pinned to `finding_output_schema()` —
   the single source of truth for the agent's response contract.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import get_args

import pytest
from sqlalchemy import select

from app.core.vcs import VCSPullRequest
from app.domain.reviewer import ReportedFinding, finding_output_schema, publish_findings
from app.domain.reviewer.models import FindingRow
from app.domain.reviewer.types import Confidence, Severity
from app.domain.tickets import create_from_pr as create_ticket
from app.domain.tickets import upsert as upsert_pr
from app.testing.stub_vcs import register_stub_vcs


def _conforming(category: str = "security", severity: str = "blocker") -> ReportedFinding:
    return ReportedFinding(
        file="src/foo.py",
        line=10,
        category=category,
        severity=severity,
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


# ── Enum gate ──────────────────────────────────────────────────────────────


@pytest.mark.service
@pytest.mark.asyncio
async def test_publish_findings_rejects_out_of_range_severity(db_session) -> None:  # type: ignore[no-untyped-def]
    """Conversion validates the raw severity string into `Severity`; an
    out-of-range value raises (the runtime gate, surfaced as a clean review
    failure by the caller).
    """
    org_id, pr_id, pr_external_id, vcs_plugin_id = await _seed_pr(db_session)

    bad = _conforming()
    bad = bad.model_copy(update={"severity": "major"})  # legacy 4-tier name, no longer valid

    with register_stub_vcs(plugin_id="github"):
        with pytest.raises(ValueError, match="severity"):
            await publish_findings(
                pr_id=pr_id,
                org_id=org_id,
                pr_external_id=pr_external_id,
                vcs_plugin_id=vcs_plugin_id,
                findings=[bad],
                session=db_session,
            )


@pytest.mark.service
@pytest.mark.asyncio
async def test_publish_findings_rejects_out_of_range_confidence(db_session) -> None:  # type: ignore[no-untyped-def]
    org_id, pr_id, pr_external_id, vcs_plugin_id = await _seed_pr(db_session)

    bad = _conforming()
    bad = bad.model_copy(update={"confidence": "totally-sure"})

    with register_stub_vcs(plugin_id="github"):
        with pytest.raises(ValueError, match="confidence"):
            await publish_findings(
                pr_id=pr_id,
                org_id=org_id,
                pr_external_id=pr_external_id,
                vcs_plugin_id=vcs_plugin_id,
                findings=[bad],
                session=db_session,
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
    `finding_display_id` values — the assignment iterates `max+i`, not `max+1`
    for every row. (Cross-transaction uniqueness is enforced by the
    `(pr_id, finding_display_id)` unique constraint on the table.)
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


def test_reported_finding_field_set_matches_finding_output_schema() -> None:
    """`ReportedFinding`'s field set is the same as the canonical schema's
    `findings[*]` items. This pin catches accidental drift between the
    lenient raw twin (`ReportedFinding`) and the strict agent-emit schema
    (`finding_output_schema()`).
    """
    schema = finding_output_schema()
    # FindingDraftList shape: {"findings": [{<item>}]}
    item_ref = schema["properties"]["findings"]["items"]
    item_schema = schema["$defs"][item_ref["$ref"].rsplit("/", 1)[-1]]
    schema_fields = set(item_schema["properties"].keys())

    reported_fields = set(ReportedFinding.model_fields.keys())
    assert reported_fields == schema_fields, (
        f"drift: ReportedFinding has {reported_fields - schema_fields}, "
        f"schema has {schema_fields - reported_fields}"
    )


# ── VCS post_finding transport ─────────────────────────────────────────────


@pytest.mark.service
@pytest.mark.asyncio
async def test_publish_findings_calls_post_finding_once_per_finding(db_session) -> None:  # type: ignore[no-untyped-def]
    """Each `ReportedFinding` maps to exactly one `vcs.post_finding` call.

    Two conforming findings → two entries in `stub.posted_findings`.
    """
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
    """A finding with no `file`/`line` (null anchor) still calls `post_finding`.

    The stub records the call regardless of anchor; the GitHub plugin routes
    null-anchor findings to the issue-comments endpoint — tested separately
    in the plugin's own unit tests.
    """
    org_id, pr_id, pr_external_id, vcs_plugin_id = await _seed_pr(db_session)

    null_anchor = ReportedFinding(
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


def test_reported_finding_severity_and_confidence_match_typed_aliases() -> None:
    """The wire enum values for `severity` and `confidence` in the schema must
    equal the `Severity`/`Confidence` `Literal` tuples on the reviewer side.
    Any drift between the agent-emit contract and the reviewer's validator
    breaks publish at runtime.
    """
    schema = finding_output_schema()
    item_ref = schema["properties"]["findings"]["items"]
    item_schema = schema["$defs"][item_ref["$ref"].rsplit("/", 1)[-1]]

    schema_sev = set(item_schema["properties"]["severity"]["enum"])
    schema_conf = set(item_schema["properties"]["confidence"]["enum"])
    assert schema_sev == set(get_args(Severity)), (
        f"severity: schema {schema_sev} vs typed {get_args(Severity)}"
    )
    assert schema_conf == set(get_args(Confidence)), (
        f"confidence: schema {schema_conf} vs typed {get_args(Confidence)}"
    )
