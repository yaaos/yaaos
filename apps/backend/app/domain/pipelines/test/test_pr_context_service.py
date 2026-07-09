"""Service test: PRContext assembly — `prev_reviewed_head_sha` derivation
across two runs.

The coding-agent stub's `compile_invocation` returns a fixed
`argv=["stub"]` exec block, discarding `Invocation.context` entirely — so
`StageInvocationContext.pr` is never observable off the wire in a stubbed
run. This drives the engine's own PRContext builder directly
(`engine._build_pr_context` / `engine._prev_reviewed_head_sha`) — intra-module
reach, permitted from this module's own test directory.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.core.audit_log import Actor
from app.core.tenancy import create_org
from app.core.vcs import VCSPullRequest
from app.domain.pipelines import Kickoff
from app.domain.pipelines.engine import _build_pr_context, _prev_reviewed_head_sha
from app.domain.pipelines.models import PipelineRunRow
from app.domain.tickets import attach_pr_to_ticket, create_from_pr
from app.domain.tickets import get as get_ticket
from app.domain.tickets import upsert as upsert_pull_request

pytestmark = pytest.mark.service


def _wire_pr(external_id: str, *, head_sha: str, base_sha: str) -> VCSPullRequest:
    now = datetime.now(UTC)
    return VCSPullRequest(
        plugin_id="github",
        external_id=external_id,
        repo_external_id="acme/repo",
        number=1,
        title="test pr",
        body=None,
        author_login="alice",
        author_type="user",
        base_branch="main",
        head_branch="feature",
        base_sha=base_sha,
        head_sha=head_sha,
        is_draft=False,
        is_fork=False,
        state="open",
        html_url="https://example.test/pr/1",
        created_at=now,
        updated_at=now,
    )


async def _seed_pr_ticket(db_session) -> tuple[UUID, UUID]:
    org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Test Org")
    ticket_id, _ = await create_from_pr(
        org_id=org.org_id,
        source_external_id=f"ext-{uuid4().hex[:8]}",
        title="pr context test ticket",
        description=None,
        repo_external_id="acme/repo",
        plugin_id="github",
        idempotency_key=f"key-{uuid4().hex}",
        payload={},
        session=db_session,
    )
    wire_pr = _wire_pr(f"acme/repo#{uuid4().hex[:4]}", head_sha="sha-first", base_sha="sha-base")
    pr_row = await upsert_pull_request(wire_pr, ticket_id=ticket_id, org_id=org.org_id, session=db_session)
    await attach_pr_to_ticket(ticket_id, org_id=org.org_id, pr_id=pr_row.id, session=db_session)
    await db_session.flush()
    return org.org_id, ticket_id


def _run_row(
    *,
    org_id: UUID,
    ticket_id: UUID,
    state: str,
    pr_head_sha: str | None,
    completed_at: datetime | None = None,
) -> PipelineRunRow:
    kickoff = Kickoff(
        intake_point_id="test",
        actor=Actor.system(),
        input_text=None,
        pr_head_sha=pr_head_sha,
        pr_base_sha="sha-base" if pr_head_sha is not None else None,
    )
    return PipelineRunRow(
        org_id=org_id,
        ticket_id=ticket_id,
        pipeline_id=None,
        pipeline_name="test-pipeline",
        definition_snapshot={"stages": []},
        state=state,
        kickoff=kickoff.model_dump(mode="json"),
        completed_at=completed_at,
    )


@pytest.mark.asyncio
async def test_prev_reviewed_head_sha_none_on_first_review(db_session) -> None:
    org_id, ticket_id = await _seed_pr_ticket(db_session)
    current_run = _run_row(org_id=org_id, ticket_id=ticket_id, state="running", pr_head_sha="sha-first")
    db_session.add(current_run)
    await db_session.flush()

    result = await _prev_reviewed_head_sha(ticket_id, exclude_run_id=current_run.id, session=db_session)
    assert result is None


@pytest.mark.asyncio
async def test_prev_reviewed_head_sha_derives_from_last_completed_pr_run(db_session) -> None:
    org_id, ticket_id = await _seed_pr_ticket(db_session)

    first_run = _run_row(
        org_id=org_id,
        ticket_id=ticket_id,
        state="completed",
        pr_head_sha="sha-first",
        completed_at=datetime.now(UTC),
    )
    db_session.add(first_run)
    await db_session.flush()

    second_run = _run_row(org_id=org_id, ticket_id=ticket_id, state="running", pr_head_sha="sha-second")
    db_session.add(second_run)
    await db_session.flush()

    result = await _prev_reviewed_head_sha(ticket_id, exclude_run_id=second_run.id, session=db_session)
    assert result == "sha-first"

    ticket = await get_ticket(ticket_id, org_id=org_id)
    second_kickoff = Kickoff.model_validate(second_run.kickoff)
    pr_context = await _build_pr_context(second_run, second_kickoff, ticket, session=db_session)
    assert pr_context is not None
    assert pr_context.head_sha == "sha-second"
    assert pr_context.base_sha == "sha-base"
    assert pr_context.prev_reviewed_head_sha == "sha-first"


@pytest.mark.asyncio
async def test_build_pr_context_none_without_pr_kickoff(db_session) -> None:
    """A run whose own kickoff didn't pin a head SHA (e.g. a non-PR-triggered
    run on a PR ticket) never gets a PRContext, even though the ticket has a
    bound PR."""
    org_id, ticket_id = await _seed_pr_ticket(db_session)
    run = _run_row(org_id=org_id, ticket_id=ticket_id, state="running", pr_head_sha=None)
    db_session.add(run)
    await db_session.flush()

    ticket = await get_ticket(ticket_id, org_id=org_id)
    kickoff = Kickoff.model_validate(run.kickoff)
    pr_context = await _build_pr_context(run, kickoff, ticket, session=db_session)
    assert pr_context is None
