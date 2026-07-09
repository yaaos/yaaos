"""Service test: `github:update_pr` driven end-to-end through the run engine
against a live `apps/fake-github` subprocess.

Acceptance: a PR-review-shaped pipeline (`ReviewSkillStage` -> `ActionStage
(github:update_pr)`) on an externally-authored PR ticket posts residual
findings to the real PR with `external_comment_id` anchored; a follow-up
`github:pr_commits`-shaped run whose review verdicts `fixed` one finding
resolves it in `domain/findings` AND calls `resolve_finding_thread` against
the fake (verified via the fake's own GraphQL `reviewThreads` query, the
same lookup `GitHubPlugin.resolve_finding_thread` performs).

`github:create_pr`'s own idempotency and posting reconciliation after a
simulated mid-action crash are covered where the entry point actually lives
— `apps/backend/app/plugins/github/test/test_pr_actions_service.py` — since
driving `Action.execute` directly needs a submodule import only permitted
from that module's own test directory.

Uses the shared `drain` outbox-dispatch helper (`test/drain.py`).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest

from app.core.agent_gateway import AgentEvent, AgentEventKind, record_agent_event
from app.core.audit_log import Actor, ActorKind
from app.core.auth import org_context
from app.core.config import get_settings
from app.core.vcs import fetch_pr, get_installation_token
from app.core.workspace import is_workspace_provider_registered, register_workspace_providers
from app.domain.findings import get as get_finding
from app.domain.findings import list_open_for_ticket
from app.domain.orgs import create_org
from app.domain.pipelines import (
    ActionStage,
    BoundaryControl,
    Kickoff,
    PipelineDefinition,
    ReviewSkillStage,
    create_pipeline,
    start_run,
)
from app.domain.pipelines.models import PipelineRunRow
from app.domain.pipelines.test.drain import drain
from app.domain.tickets import attach_pr_to_ticket, create_from_pr
from app.domain.tickets import upsert as upsert_pull_request
from app.plugins.github import GitHubPlugin, record_app_install
from app.testing.e2e_setup import seed_agent

pytestmark = [pytest.mark.service, pytest.mark.usefixtures("redis_or_skip")]

_REPO_EXTERNAL_ID = "acme/web"
_PR_EXTERNAL_ID = "acme/web#1"  # apps/fake-github's default-seeded PR
_STAGE_NAME = "code-review"


@pytest.fixture
async def github_org(db_session, monkeypatch: pytest.MonkeyPatch, fake_github_base_url: str) -> UUID:
    """An org with an active GitHub App installation pointed at a live
    `apps/fake-github` subprocess — mirrors
    `app/core/vcs/test/test_write_ops_against_fake_github.py`'s `github_org`."""
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


def _review_pipeline() -> PipelineDefinition:
    return PipelineDefinition(
        name=f"pr-review-{uuid4().hex[:8]}",
        stages=(
            ReviewSkillStage(
                name=_STAGE_NAME,
                skill_name=_STAGE_NAME,
                coding_agent_plugin_id="claude_code",
                model="sonnet",
                effort="medium",
                boundary=BoundaryControl(mode="always_proceed"),
            ),
            ActionStage(description="post findings", action_id="github:update_pr"),
        ),
    )


async def _seed_pr_ticket(org_id: UUID, db_session) -> UUID:
    """Ticket bound to fake-github's pre-seeded `acme/web#1` — an
    externally-authored PR, matching the "team reviews externally-authored
    PRs" flow (ticket.pr_id set from creation, never via `create_pr`)."""
    wire_pr = await fetch_pr("github", org_id, _PR_EXTERNAL_ID)
    ticket_id, _ = await create_from_pr(
        org_id=org_id,
        source_external_id=_PR_EXTERNAL_ID,
        title=wire_pr.title,
        description=wire_pr.body,
        repo_external_id=_REPO_EXTERNAL_ID,
        plugin_id="github",
        idempotency_key=f"key-{uuid4().hex}",
        payload={},
        branch_name=wire_pr.head_branch,
        session=db_session,
    )
    pr_row = await upsert_pull_request(wire_pr, ticket_id=ticket_id, org_id=org_id, session=db_session)
    await attach_pr_to_ticket(ticket_id, org_id=org_id, pr_id=pr_row.id, session=db_session)
    await db_session.flush()
    return ticket_id


def _success_event(command_id: UUID, *, outputs: dict) -> AgentEvent:
    return AgentEvent(
        command_id=command_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        outcome_label="success",
        outputs=outputs,
        reported_at=datetime.now(UTC),
        traceparent="",
    )


async def _record(org_id: UUID, event: AgentEvent, *, agent_id: UUID | None, db_session) -> None:
    async with org_context(org_id, ActorKind.WORKSPACE, actor_id=None):
        await record_agent_event(event, agent_id=agent_id, session=db_session)
    await db_session.commit()


async def _run_review_and_action(
    org_id: UUID,
    ticket_id: UUID,
    *,
    intake_point_id: str,
    head_sha: str,
    base_sha: str,
    review_output: dict,
    db_session,
) -> UUID:
    """Drive one full run (provision -> review stage -> action stage ->
    cleanup -> completed) on an already-bound PR ticket. Returns `run_id`."""
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()
    agent_row = await seed_agent(org_id=org_id)

    pipeline_id = await create_pipeline(
        org_id=org_id, definition=_review_pipeline(), actor=Actor.system(), session=db_session
    )
    await db_session.flush()

    kickoff = Kickoff(
        intake_point_id=intake_point_id,
        actor=Actor.system(),
        input_text=None,
        pr_head_sha=head_sha,
        pr_base_sha=base_sha,
    )
    run_id = await start_run(
        org_id=org_id, ticket_id=ticket_id, pipeline_id=pipeline_id, kickoff=kickoff, session=db_session
    )
    await db_session.commit()
    await drain(db_session)

    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    assert run.phase == "provision"
    provision_command_id = run.pending_agent_command_id
    assert provision_command_id is not None
    await _record(
        org_id,
        _success_event(provision_command_id, outputs={}),
        agent_id=agent_row["id"],
        db_session=db_session,
    )
    await drain(db_session)

    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    review_command_id = run.pending_agent_command_id
    assert review_command_id is not None
    await _record(
        org_id,
        _success_event(review_command_id, outputs={"stdout": json.dumps(review_output), "exit_code": 0}),
        agent_id=None,
        db_session=db_session,
    )
    await drain(db_session)  # runs the action stage synchronously, then dispatches cleanup

    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    assert run.phase == "cleanup"
    cleanup_command_id = run.pending_agent_command_id
    assert cleanup_command_id is not None
    await _record(
        org_id, _success_event(cleanup_command_id, outputs={}), agent_id=None, db_session=db_session
    )
    await drain(db_session)

    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    assert run.state == "completed", run.failure_reason
    return run_id


async def _thread_resolved(base_url: str, headers: dict[str, str], comment_external_id: str) -> bool:
    """Directly queries the fake's GraphQL `reviewThreads` — mirrors
    `GitHubPlugin.resolve_finding_thread`'s own lookup — to prove the thread
    anchoring `comment_external_id` is resolved."""
    query = """
    query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $number) {
          reviewThreads(first: 100) {
            nodes { id isResolved comments(first: 50) { nodes { databaseId } } }
          }
        }
      }
    }
    """
    async with httpx.AsyncClient(base_url=base_url, timeout=15) as client:
        resp = await client.post(
            "/graphql",
            json={"query": query, "variables": {"owner": "acme", "repo": "web", "number": 1}},
            headers=headers,
        )
    resp.raise_for_status()
    nodes = resp.json()["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    target_id = int(comment_external_id)
    thread = next(
        (n for n in nodes if any(c["databaseId"] == target_id for c in n["comments"]["nodes"])), None
    )
    assert thread is not None, f"no review thread found for comment {comment_external_id}"
    return bool(thread["isResolved"])


@pytest.mark.asyncio
async def test_acceptance_residuals_posted_and_incremental_review_resolves(
    github_org: UUID, fake_github_base_url: str, db_session
) -> None:
    org_id = github_org
    ticket_id = await _seed_pr_ticket(org_id, db_session)
    wire_pr = await fetch_pr("github", org_id, _PR_EXTERNAL_ID)

    # First run ("github:pr_opened"-shaped): review reports a blocker (inline,
    # code_file+code_line — anchors a real review thread) and a nit.
    await _run_review_and_action(
        org_id,
        ticket_id,
        intake_point_id="github:pr_opened",
        head_sha=wire_pr.head_sha,
        base_sha=wire_pr.base_sha,
        review_output={
            "new_findings": [
                {
                    "category": "sec",
                    "severity": "blocker",
                    "body": "SQL injection risk",
                    "code_file": "app.py",
                    "code_line": 10,
                },
                {"category": "code", "severity": "nit", "body": "naming nit"},
            ],
            "prior_finding_verdicts": [],
            "confidence": 80,
            "summary": "found issues",
        },
        db_session=db_session,
    )

    findings = await list_open_for_ticket(org_id, ticket_id, session=db_session)
    assert sorted(f.handle for f in findings) == ["code-002", "sec-001"]
    assert all(f.external_comment_id is not None for f in findings)
    blocker = next(f for f in findings if f.severity == "blocker")

    # Verify against fake-github directly: the anchored comment actually
    # carries the finding's own handle.
    token = await get_installation_token("github", org_id)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    async with httpx.AsyncClient(base_url=fake_github_base_url, timeout=15) as client:
        resp = await client.get("/repos/acme/web/pulls/1/comments", headers=headers)
    resp.raise_for_status()
    posted_bodies = [c["body"] for c in resp.json()]
    assert any(blocker.handle in body for body in posted_bodies)

    # Second run ("github:pr_commits"-shaped): review returns no new findings
    # but asserts a "fixed" verdict on the blocker.
    await _run_review_and_action(
        org_id,
        ticket_id,
        intake_point_id="github:pr_commits",
        head_sha="second-commit-sha",
        base_sha=wire_pr.base_sha,
        review_output={
            "new_findings": [],
            "prior_finding_verdicts": [{"finding_id": str(blocker.id), "status": "fixed"}],
            "confidence": 90,
            "summary": "blocker fixed",
        },
        db_session=db_session,
    )

    resolved_blocker = await get_finding(blocker.id, session=db_session)
    assert resolved_blocker.status == "resolved"
    assert resolved_blocker.status_events[-1].method == "review_verdict"

    assert await _thread_resolved(fake_github_base_url, headers, blocker.external_comment_id)
