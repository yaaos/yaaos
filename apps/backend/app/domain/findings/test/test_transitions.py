"""Unit-tier test for `domain/findings`: the full status-transition matrix
(`open -> resolved/dismissed`, `resolved -> open/dismissed`, `dismissed`
terminal), idempotent re-assertion (no duplicate event on a same-status
call), `record_findings`' idempotency-on-id, and per-ticket monotonic
`display_id`/handle assignment.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid7

import pytest

from app.core.audit_log import Actor
from app.core.tenancy import create_org
from app.domain.findings import (
    FindingSpec,
    FindingStatusEvent,
    InvalidFindingTransition,
    dismiss,
    list_for_stage_execution,
    record_findings,
    reflag,
    reopen,
    resolve,
)
from app.domain.tickets import create_from_pr

pytestmark = pytest.mark.asyncio


async def _seed_ticket(db_session) -> tuple[UUID, UUID]:
    org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Test Org")
    ticket_id, _ = await create_from_pr(
        org_id=org.org_id,
        source_external_id=f"ext-{uuid4().hex[:8]}",
        title="findings test ticket",
        description=None,
        repo_external_id="acme/repo",
        plugin_id="github",
        idempotency_key=f"key-{uuid4().hex}",
        payload={},
        session=db_session,
    )
    await db_session.flush()
    return org.org_id, ticket_id


def _spec(**overrides: object) -> FindingSpec:
    defaults: dict[str, object] = {
        "id": uuid7(),
        "severity": "should_fix",
        "body": "body",
        "code_file": None,
        "code_line": None,
        "artifact_section": None,
        "defect_in_artifact": None,
        "display_prefix": "SPEC",
    }
    defaults.update(overrides)
    return FindingSpec(**defaults)  # type: ignore[arg-type]


def _event(status: str, *, method: str = "review_verdict") -> FindingStatusEvent:
    return FindingStatusEvent(status=status, method=method, actor=Actor.system(), at=datetime.now(UTC))  # type: ignore[arg-type]


async def test_record_findings_materializes_open_with_handle(db_session) -> None:
    org_id, ticket_id = await _seed_ticket(db_session)
    [finding] = await record_findings(
        org_id=org_id,
        ticket_id=ticket_id,
        run_id=uuid7(),
        stage_name="review",
        stage_execution_id=uuid7(),
        iteration=1,
        findings=[_spec()],
        session=db_session,
    )
    assert finding.status == "open"
    assert finding.handle == "SPEC-001"


async def test_display_id_monotonic_per_ticket(db_session) -> None:
    org_id, ticket_id = await _seed_ticket(db_session)
    recorded = await record_findings(
        org_id=org_id,
        ticket_id=ticket_id,
        run_id=uuid7(),
        stage_name="review",
        stage_execution_id=uuid7(),
        iteration=1,
        findings=[_spec(), _spec()],
        session=db_session,
    )
    assert [f.handle for f in recorded] == ["SPEC-001", "SPEC-002"]


async def test_record_findings_idempotent_on_id_refreshes_body_severity_immutable(db_session) -> None:
    org_id, ticket_id = await _seed_ticket(db_session)
    stage_execution_id = uuid7()
    run_id = uuid7()
    finding_id = uuid7()

    await record_findings(
        org_id=org_id,
        ticket_id=ticket_id,
        run_id=run_id,
        stage_name="review",
        stage_execution_id=stage_execution_id,
        iteration=1,
        findings=[_spec(id=finding_id, body="first body")],
        session=db_session,
    )
    await record_findings(
        org_id=org_id,
        ticket_id=ticket_id,
        run_id=run_id,
        stage_name="review",
        stage_execution_id=stage_execution_id,
        iteration=2,
        # severity differs — must NOT flip; body/code_line must refresh.
        findings=[_spec(id=finding_id, severity="blocker", body="revised body", code_line=42)],
        session=db_session,
    )

    [refetched] = await list_for_stage_execution(stage_execution_id, session=db_session)
    assert refetched.id == finding_id
    assert refetched.body == "revised body"
    assert refetched.code_line == 42
    assert refetched.severity == "should_fix"


async def test_full_transition_matrix_and_dismissed_terminal(db_session) -> None:
    org_id, ticket_id = await _seed_ticket(db_session)
    stage_execution_id = uuid7()
    [finding] = await record_findings(
        org_id=org_id,
        ticket_id=ticket_id,
        run_id=uuid7(),
        stage_name="review",
        stage_execution_id=stage_execution_id,
        iteration=1,
        findings=[_spec()],
        session=db_session,
    )

    await resolve(finding.id, event=_event("resolved"), session=db_session)
    [after_resolve] = await list_for_stage_execution(stage_execution_id, session=db_session)
    assert after_resolve.status == "resolved"

    await reopen(finding.id, event=_event("open"), session=db_session)
    [after_reopen] = await list_for_stage_execution(stage_execution_id, session=db_session)
    assert after_reopen.status == "open"

    await dismiss(finding.id, event=_event("dismissed", method="user_overrode"), session=db_session)
    [after_dismiss] = await list_for_stage_execution(stage_execution_id, session=db_session)
    assert after_dismiss.status == "dismissed"

    # dismissed is terminal — every outbound transition is illegal.
    with pytest.raises(InvalidFindingTransition):
        await reopen(finding.id, event=_event("open"), session=db_session)
    with pytest.raises(InvalidFindingTransition):
        await resolve(finding.id, event=_event("resolved"), session=db_session)
    with pytest.raises(InvalidFindingTransition):
        await reflag(finding.id, event=_event("open"), session=db_session)


async def test_idempotent_same_status_is_no_op_no_duplicate_event(db_session) -> None:
    org_id, ticket_id = await _seed_ticket(db_session)
    stage_execution_id = uuid7()
    [finding] = await record_findings(
        org_id=org_id,
        ticket_id=ticket_id,
        run_id=uuid7(),
        stage_name="review",
        stage_execution_id=stage_execution_id,
        iteration=1,
        findings=[_spec()],
        session=db_session,
    )

    await resolve(finding.id, event=_event("resolved"), session=db_session)
    await resolve(finding.id, event=_event("resolved"), session=db_session)  # idempotent no-op

    [refetched] = await list_for_stage_execution(stage_execution_id, session=db_session)
    assert refetched.status == "resolved"
    assert len(refetched.status_events) == 1


async def test_reflag_appends_event_and_requires_open(db_session) -> None:
    org_id, ticket_id = await _seed_ticket(db_session)
    stage_execution_id = uuid7()
    [finding] = await record_findings(
        org_id=org_id,
        ticket_id=ticket_id,
        run_id=uuid7(),
        stage_name="review",
        stage_execution_id=stage_execution_id,
        iteration=1,
        findings=[_spec()],
        session=db_session,
    )

    await reflag(finding.id, event=_event("open"), session=db_session)
    [refetched] = await list_for_stage_execution(stage_execution_id, session=db_session)
    assert refetched.status == "open"
    assert len(refetched.status_events) == 1
    assert refetched.status_events[0].method == "review_verdict"

    await resolve(finding.id, event=_event("resolved"), session=db_session)
    with pytest.raises(InvalidFindingTransition):
        await reflag(finding.id, event=_event("open"), session=db_session)
