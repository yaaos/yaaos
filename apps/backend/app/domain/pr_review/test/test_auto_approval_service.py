"""Service test: PR auto-approval (`domain/pr_review.evaluate_auto_approval`).

Uses `app.testing.stub_vcs` — the Acceptance sentence only needs the
recorded `approve_pr` call + `has_active_approval` state, not a live
GitHub API round trip. Findings are seeded directly via
`domain/findings.record_findings` + the transition functions rather than
driving a full pipeline run — `evaluate_auto_approval` reads durable ticket
+ repo-settings + finding state only, so a full run isn't load-bearing for
this behavior.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid7

import pytest

from app.core.audit_log import Actor
from app.core.audit_log import list_for_entity as list_audit_for_entity
from app.core.tenancy import create_org
from app.domain.findings import FindingSpec, FindingStatusEvent, dismiss, record_findings, resolve
from app.domain.findings import set_external_anchor as anchor_finding
from app.domain.pr_review import evaluate_auto_approval
from app.domain.repos import RepoSettingsSpec, put_settings
from app.domain.tickets import attach_pr_to_ticket, create_from_pr
from app.domain.tickets import upsert as upsert_pull_request
from app.testing.stub_vcs import StubVCSPlugin, register_stub_vcs

pytestmark = [pytest.mark.asyncio, pytest.mark.service]

_REPO_EXTERNAL_ID = "owner/repo"
_PR_EXTERNAL_ID = "owner/repo#1"  # StubVCSPlugin's default PR


async def _seed_pr_ticket(
    org_id: UUID, stub: StubVCSPlugin, db_session, *, branch_name: str | None = None
) -> tuple[UUID, UUID]:
    """Seeds a ticket + its linked PR row. Returns `(ticket_id, pr_row_id)`.
    `branch_name=None` uses the PR's own head branch (an externally-opened
    PR ticket); passing a `yaaos/...` branch simulates a yaaos-authored
    ticket whose stage pushed to its own minted branch."""
    wire_pr = await stub.fetch_pr(org_id, _PR_EXTERNAL_ID)
    ticket_id, _ = await create_from_pr(
        org_id=org_id,
        source_external_id=wire_pr.external_id,
        title=wire_pr.title,
        description=wire_pr.body,
        repo_external_id=_REPO_EXTERNAL_ID,
        plugin_id="github",
        idempotency_key=f"key-{uuid4().hex}",
        payload={},
        branch_name=branch_name if branch_name is not None else wire_pr.head_branch,
        session=db_session,
    )
    pr_row = await upsert_pull_request(wire_pr, ticket_id=ticket_id, org_id=org_id, session=db_session)
    await attach_pr_to_ticket(ticket_id, org_id=org_id, pr_id=pr_row.id, session=db_session)
    await db_session.flush()
    return ticket_id, pr_row.id


async def _enable_auto_approve(org_id: UUID, *, conditions: dict[str, bool], db_session) -> None:
    await put_settings(
        org_id,
        _REPO_EXTERNAL_ID,
        settings=RepoSettingsSpec(auto_approve_enabled=True, auto_approve_conditions=conditions),
        actor=Actor.system(),
        session=db_session,
    )


def _event(status: str, *, method: str = "review_verdict") -> FindingStatusEvent:
    return FindingStatusEvent(status=status, method=method, actor=Actor.system(), at=datetime.now(UTC))  # type: ignore[arg-type]


async def _seed_posted_finding(org_id: UUID, ticket_id: UUID, *, severity: str, db_session):
    [finding] = await record_findings(
        org_id=org_id,
        ticket_id=ticket_id,
        run_id=uuid7(),
        stage_name="review",
        stage_execution_id=uuid7(),
        iteration=1,
        findings=[FindingSpec(id=uuid7(), severity=severity, body="body", display_prefix="SPEC")],  # type: ignore[arg-type]
        session=db_session,
    )
    await anchor_finding(finding.id, comment_external_id=f"c-{uuid4().hex[:6]}", session=db_session)
    return finding


async def test_approves_once_blocker_resolved_not_before_not_twice_and_reapproves_after_dismiss(
    db_session,
) -> None:
    with register_stub_vcs(plugin_id="github") as stub:
        org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Auto Approve Org")
        org_id = org.org_id
        ticket_id, _pr_id = await _seed_pr_ticket(org_id, stub, db_session)
        await _enable_auto_approve(org_id, conditions={"no_blocker": True}, db_session=db_session)
        finding = await _seed_posted_finding(org_id, ticket_id, severity="blocker", db_session=db_session)

        # Blocker still open -> conditions fail -> not approved.
        await evaluate_auto_approval(org_id, ticket_id, session=db_session)
        assert stub.approved_prs == []

        # Resolve the blocker -> conditions pass -> approved.
        await resolve(finding.id, event=_event("resolved"), session=db_session)
        await evaluate_auto_approval(org_id, ticket_id, session=db_session)
        assert stub.approved_prs == [(org_id, _PR_EXTERNAL_ID)]

        # Called again while GitHub still reports the approval active -> no duplicate call.
        await evaluate_auto_approval(org_id, ticket_id, session=db_session)
        assert stub.approved_prs == [(org_id, _PR_EXTERNAL_ID)]

        # Dismiss-on-push: a new push drops the stale review; GitHub is the
        # source of truth, so the next terminal re-approves with no local marker.
        stub.set_active_approval(_PR_EXTERNAL_ID, False)
        await evaluate_auto_approval(org_id, ticket_id, session=db_session)
        assert stub.approved_prs == [(org_id, _PR_EXTERNAL_ID), (org_id, _PR_EXTERNAL_ID)]


async def test_dismissed_finding_does_not_block_all_confirmed_fixed(db_session) -> None:
    """A dismissed finding is excluded from `all_confirmed_fixed`'s
    resolved-check entirely — it doesn't need to have been resolved for the
    condition to pass, and it doesn't block approval on its own."""
    with register_stub_vcs(plugin_id="github") as stub:
        org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Auto Approve Org 2")
        org_id = org.org_id
        ticket_id, _pr_id = await _seed_pr_ticket(org_id, stub, db_session)
        await _enable_auto_approve(org_id, conditions={"all_confirmed_fixed": True}, db_session=db_session)
        finding = await _seed_posted_finding(org_id, ticket_id, severity="nit", db_session=db_session)
        await dismiss(finding.id, event=_event("dismissed", method="user_overrode"), session=db_session)

        await evaluate_auto_approval(org_id, ticket_id, session=db_session)
        assert stub.approved_prs == [(org_id, _PR_EXTERNAL_ID)]


async def test_yaaos_authored_pr_is_skipped_with_audit_reason(db_session) -> None:
    with register_stub_vcs(plugin_id="github") as stub:
        org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Auto Approve Org 3")
        org_id = org.org_id
        ticket_id, pr_id = await _seed_pr_ticket(
            org_id, stub, db_session, branch_name="yaaos/some-dev-ticket-12345678"
        )
        await _enable_auto_approve(org_id, conditions={"no_blocker": True}, db_session=db_session)

        await evaluate_auto_approval(org_id, ticket_id, session=db_session)

        assert stub.approved_prs == []
        entries = await list_audit_for_entity("pull_request", pr_id, org_id=org_id)
        assert any(e.kind == "pull_request.auto_approve_skipped" for e in entries)


async def test_disabled_repo_never_approves(db_session) -> None:
    with register_stub_vcs(plugin_id="github") as stub:
        org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Auto Approve Org 4")
        org_id = org.org_id
        ticket_id, _pr_id = await _seed_pr_ticket(org_id, stub, db_session)
        # No `put_settings` call at all -> repo defaults to auto_approve_enabled=False.

        await evaluate_auto_approval(org_id, ticket_id, session=db_session)
        assert stub.approved_prs == []
