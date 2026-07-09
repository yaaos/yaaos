"""Service test: `GitHubCreatePRAction`/`_post_residuals` idempotency and
posting-reconciliation, driven directly (not through the run engine) against
a live `apps/fake-github` subprocess.

Both scenarios simulate a mid-body crash: a GitHub-side write (PR opened, or
a finding comment posted) survives, but the DB write recording it was rolled
back with the rest of the uncommitted transaction. A retry must resolve the
SAME external state rather than duplicating it — `vcs.create_pr`'s
find-existing-for-branch fallback for the PR, `vcs.list_yaaos_comments` +
the finding's own `handle` for the comment.

Entry point is `Action.execute`/`_post_residuals`, owned by this module —
direct submodule imports (`app.plugins.github.actions`,
`app.plugins.github.service`) are intra-module and permitted from this
test directory. The full-engine acceptance flow (residuals posted +
incremental review resolves a thread) lives at
`apps/backend/app/domain/pipelines/test/test_pr_actions_service.py`, whose
entry point is `pipelines.start_run`.
"""

from __future__ import annotations

from uuid import UUID, uuid4, uuid7

import httpx
import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.vcs import get_installation_token
from app.domain.actions import ActionContext
from app.domain.findings import FindingSpec, record_findings
from app.domain.findings import get as get_finding
from app.domain.orgs import create_org
from app.domain.tickets import create_from_pr
from app.domain.tickets import get as get_ticket
from app.plugins.github.actions import GitHubCreatePRAction, _post_residuals
from app.plugins.github.service import GitHubPlugin, record_app_install

pytestmark = [pytest.mark.service, pytest.mark.usefixtures("redis_or_skip")]

_REPO_EXTERNAL_ID = "acme/web"
_PR_EXTERNAL_ID = "acme/web#1"  # apps/fake-github's default-seeded PR


@pytest.fixture
async def github_org(db_session, monkeypatch: pytest.MonkeyPatch, fake_github_base_url: str) -> UUID:
    """An org with an active GitHub App installation pointed at a live
    `apps/fake-github` subprocess."""
    org = await create_org(
        db_session, slug=f"pr-actions-org-{uuid4().hex[:8]}", display_name="PR Actions Org"
    )
    await record_app_install(
        db_session, org_id=org.id, install_external_id="install-fake-1", account_login="acme"
    )
    await db_session.flush()

    monkeypatch.setenv("YAAOS_GITHUB_APP_ID", "12345")
    monkeypatch.setenv("YAAOS_GITHUB_APP_PRIVATE_KEY", "fake-pem-not-a-real-key")
    monkeypatch.setenv("YAAOS_GITHUB_APP_WEBHOOK_SECRET", "whsec-test")
    monkeypatch.setenv("YAAOS_GITHUB_APP_SLUG", "yaaos-test")
    get_settings.cache_clear()
    monkeypatch.setattr(GitHubPlugin, "base_url", fake_github_base_url)

    return org.id


@pytest.mark.asyncio
async def test_create_pr_idempotent_on_retry_after_lost_attach(github_org: UUID, db_session) -> None:
    """A yaaos-authored ticket (no PR yet). `github:create_pr` opens the PR
    once; simulating a crash between `vcs.create_pr` and the
    `attach_pr_to_ticket` write (DB rolled back, GitHub already has the PR),
    a retry resolves the SAME PR via `vcs.create_pr`'s find-existing
    fallback rather than opening a duplicate."""
    org_id = github_org
    ticket_id, _ = await create_from_pr(
        org_id=org_id,
        source_external_id=f"dev-ticket-{uuid4().hex[:8]}",
        title="Implement the feature",
        description=None,
        repo_external_id=_REPO_EXTERNAL_ID,
        plugin_id="github",
        idempotency_key=f"key-{uuid4().hex}",
        payload={},
        branch_name="yaaos/implement-feature-abc123",
        session=db_session,
    )
    await db_session.flush()

    action = GitHubCreatePRAction()
    ctx = ActionContext(
        org_id=org_id,
        ticket_id=ticket_id,
        run_id=uuid4(),
        repo_external_id=_REPO_EXTERNAL_ID,
        vcs_plugin_id="github",
        pr_external_id=None,
        branch_name="yaaos/implement-feature-abc123",
        intake_point_id="test",
        kickoff_input=None,
        preceding_residuals=(),
        preceding_verdicts=(),
        preceding_artifact_id=None,
    )

    first = await action.execute(ctx, session=db_session)
    await db_session.commit()
    ticket_after_first = await get_ticket(ticket_id, org_id=org_id)
    assert ticket_after_first.pr_id is not None

    # Simulate the crash: the real PR + the attach both landed above, but a
    # crash right after `vcs.create_pr` (before `attach_pr_to_ticket`
    # committed) would have left `pr_id` NULL while GitHub already has the
    # PR — reproduce that DB state directly and retry.
    await db_session.execute(text("UPDATE tickets SET pr_id = NULL WHERE id = :id"), {"id": ticket_id})
    await db_session.flush()

    second = await action.execute(ctx, session=db_session)
    await db_session.commit()

    assert second.pr_external_id == first.pr_external_id
    ticket_after_second = await get_ticket(ticket_id, org_id=org_id)
    assert ticket_after_second.pr_id is not None


@pytest.mark.asyncio
async def test_posting_reconciles_after_simulated_crash(
    github_org: UUID, fake_github_base_url: str, db_session
) -> None:
    """A finding whose GitHub comment was posted but whose
    `external_comment_id` DB write was lost (simulated crash) is discovered
    via `vcs.list_yaaos_comments` on retry — reconciled, not double-posted."""
    org_id = github_org
    ticket_id, _ = await create_from_pr(
        org_id=org_id,
        source_external_id=_PR_EXTERNAL_ID,
        title="Externally-authored PR",
        description=None,
        repo_external_id=_REPO_EXTERNAL_ID,
        plugin_id="github",
        idempotency_key=f"key-{uuid4().hex}",
        payload={},
        session=db_session,
    )
    await db_session.flush()

    finding_id = uuid7()
    recorded = await record_findings(
        org_id=org_id,
        ticket_id=ticket_id,
        run_id=uuid4(),
        stage_name="code-review",
        stage_execution_id=uuid4(),
        iteration=1,
        findings=[
            FindingSpec(
                id=finding_id,
                severity="blocker",
                body="SQL injection risk",
                code_file="app.py",
                code_line=10,
                display_prefix="SPEC",
            )
        ],
        session=db_session,
    )
    finding = recorded[0]

    ctx = ActionContext(
        org_id=org_id,
        ticket_id=ticket_id,
        run_id=uuid4(),
        repo_external_id=_REPO_EXTERNAL_ID,
        vcs_plugin_id="github",
        pr_external_id=_PR_EXTERNAL_ID,
        branch_name="feature",
        intake_point_id="test",
        kickoff_input=None,
        preceding_residuals=(finding,),
        preceding_verdicts=(),
        preceding_artifact_id=None,
    )

    first_posted = await _post_residuals(ctx, _PR_EXTERNAL_ID, session=db_session)
    assert first_posted == [finding.id]
    await db_session.commit()

    anchored = await get_finding(finding.id, session=db_session)
    assert anchored.external_comment_id is not None
    first_comment_id = anchored.external_comment_id

    # Simulate the crash: the comment is real on GitHub, but the DB write
    # anchoring it never landed.
    await db_session.execute(
        text("UPDATE findings SET external_comment_id = NULL WHERE id = :id"), {"id": finding_id}
    )
    await db_session.flush()
    finding_before_retry = await get_finding(finding.id, session=db_session)
    ctx_retry = ctx.model_copy(update={"preceding_residuals": (finding_before_retry,)})

    second_posted = await _post_residuals(ctx_retry, _PR_EXTERNAL_ID, session=db_session)
    await db_session.commit()
    assert second_posted == [finding.id]

    reconciled = await get_finding(finding.id, session=db_session)
    assert reconciled.external_comment_id == first_comment_id  # same comment, not a new one

    token = await get_installation_token("github", org_id)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    async with httpx.AsyncClient(base_url=fake_github_base_url, timeout=15) as client:
        resp = await client.get("/repos/acme/web/pulls/1/comments", headers=headers)
    resp.raise_for_status()
    bodies_matching = [c for c in resp.json() if finding.handle in c["body"]]
    assert len(bodies_matching) == 1  # not double-posted
