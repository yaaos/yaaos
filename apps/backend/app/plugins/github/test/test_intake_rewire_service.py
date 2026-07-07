"""Service test: the intake rewire — `github:pr_opened`/`github:pr_commits`
resolve `domain/repos` trigger bindings and route bound repos into
`pipelines.start_run`; unbound repos keep today's `pr_review_v1` fallback.

Covers:
- bound `pull_request.opened` → ticket (with PR-derived `branch_name`) +
  `pipeline_runs` row.
- unbound `pull_request.opened` → `pr_review_v1` workflow start, unchanged.
- bound `pull_request.synchronize` → a second run on the same ticket via
  `github:pr_commits`.
- unbound `pull_request.synchronize` → metadata refresh only, no run.
- a `start_run` config-problem exception (`PipelineNotFoundError` /
  `PipelineValidationError`) surfaces as a 2xx-with-detail outcome plus a
  `ticket.pipeline_start_failed` audit row — webhooks never retry-loop on
  config problems.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid4

import pytest

from app.core.audit_log import Actor
from app.core.audit_log import list_for_entity as list_audit_for_entity
from app.core.auth import Role
from app.core.identity import create_user
from app.core.workflow import (
    CommandContext,
    Empty,
    Outcome,
    TerminalAction,
    Workflow,
    step,
)
from app.domain.orgs import create_membership, create_org
from app.domain.pipelines import ActionStage, PipelineDefinition, create_pipeline
from app.domain.repos import TriggerBinding, TriggerBindingSpec, add_binding
from app.domain.tickets import TicketFilter, list_tickets
from app.plugins.github.intake_type import GithubIntakeType
from app.testing.workflow_harness import set_engine_for_tests


class _NoopLocal:
    kind: Literal["NoopIntakeRewire"] = "NoopIntakeRewire"
    Inputs = Empty
    Outputs = Empty
    restart_safe = True

    async def execute(self, inputs: Empty, ctx: CommandContext, *, session=None) -> Outcome:
        del inputs, ctx, session
        return Outcome.success()


_noop_step = step(_NoopLocal)


@pytest.fixture
def _stub_pr_review_engine():  # type: ignore[no-untyped-def]
    """Register a one-step `pr_review_v1` workflow so the unbound fallback's
    `engine.start(...)` resolves without pulling in the full reviewer command
    set."""
    with set_engine_for_tests() as eng:
        eng.register_workflow(
            Workflow(
                name="pr_review_v1",
                version=1,
                steps=(_noop_step,),
                entry=_noop_step,
                transitions={_noop_step: {"success": TerminalAction.COMPLETE_WORKFLOW}},
            )
        )
        yield eng


def _pr_payload(*, number: int, head_ref: str, head_sha: str, action: str = "opened") -> dict:
    pr = {
        "number": number,
        "title": "Add feature",
        "body": "Body text",
        "draft": False,
        "merged": False,
        "state": "open",
        "html_url": f"https://github.com/acme/web/pull/{number}",
        "user": {"login": "alice", "type": "User"},
        "head": {
            "ref": head_ref,
            "sha": head_sha,
            "repo": {"fork": False, "full_name": "acme/web"},
        },
        "base": {
            "ref": "main",
            "sha": "aaa",
            "repo": {"full_name": "acme/web"},
        },
        "created_at": "2026-05-01T10:00:00Z",
        "updated_at": "2026-05-01T10:00:00Z",
        "labels": [],
    }
    return {"action": action, "pull_request": pr, "repository": {"full_name": "acme/web"}}


async def _seed_org(db_session) -> UUID:  # type: ignore[no-untyped-def]
    slug = f"intake-rewire-org-{uuid4().hex[:8]}"
    org = await create_org(db_session, slug=slug, display_name="Intake Rewire Org")
    await db_session.flush()
    user = await create_user(db_session, display_name="Owner")
    await db_session.flush()
    await create_membership(
        db_session, user_id=user.id, org_id=org.id, role=Role.OWNER, handle=f"owner-{uuid4().hex[:4]}"
    )
    await db_session.commit()
    return org.id


async def _seed_pipeline(db_session, org_id: UUID) -> tuple[UUID, str]:  # type: ignore[no-untyped-def]
    name = f"pipeline-{uuid4().hex[:6]}"
    pipeline_id = await create_pipeline(
        org_id=org_id,
        definition=PipelineDefinition(name=name, stages=(ActionStage(action_id="github:create_pr"),)),
        actor=Actor.system(),
        session=db_session,
    )
    await db_session.commit()
    return pipeline_id, name


async def _only_ticket(db_session, org_id: UUID):  # type: ignore[no-untyped-def]
    tickets = await list_tickets(TicketFilter(), org_id=org_id)
    assert len(tickets) == 1, f"expected exactly one ticket, got {len(tickets)}"
    return tickets[0]


@pytest.mark.service
@pytest.mark.asyncio
async def test_bound_pr_opened_starts_pipeline_run_with_branch_name(db_session) -> None:
    org_id = await _seed_org(db_session)
    pipeline_id, pipeline_name = await _seed_pipeline(db_session, org_id)
    await add_binding(
        org_id,
        "acme/web",
        spec=TriggerBindingSpec(intake_point_id="github:pr_opened", pipeline_id=pipeline_id),
        actor=Actor.system(),
        session=db_session,
    )
    await db_session.commit()

    outcome = await GithubIntakeType()._prepare_review_or_run(
        payload=_pr_payload(number=1, head_ref="feat-branch", head_sha="ccc"),
        delivery=f"evt-{uuid4().hex}",
        org_id=org_id,
        session=db_session,
    )
    await db_session.commit()

    assert outcome.detail == "pipeline_run_started"

    ticket = await _only_ticket(db_session, org_id)
    assert ticket.branch_name == "feat-branch"
    assert ticket.current_run_id is not None

    from app.core.audit_log import ActorKind  # noqa: PLC0415
    from app.core.auth import org_context  # noqa: PLC0415
    from app.domain.pipelines import list_runs_for_ticket  # noqa: PLC0415

    async with org_context(org_id, ActorKind.SYSTEM):
        runs = await list_runs_for_ticket(ticket.id, session=db_session)
    assert len(runs) == 1
    assert runs[0].pipeline_name == pipeline_name


@pytest.mark.service
@pytest.mark.asyncio
async def test_unbound_pr_opened_still_starts_pr_review_v1(db_session, _stub_pr_review_engine) -> None:
    """No binding for `github:pr_opened` on this repo → the coexistence
    fallback (`pr_review_v1`) fires exactly as it did before the rewire."""
    org_id = await _seed_org(db_session)

    outcome = await GithubIntakeType()._prepare_review_or_run(
        payload=_pr_payload(number=2, head_ref="feat-unbound", head_sha="ddd"),
        delivery=f"evt-{uuid4().hex}",
        org_id=org_id,
        session=db_session,
    )
    await db_session.commit()

    assert outcome.detail == "pr_review_started"

    ticket = await _only_ticket(db_session, org_id)
    # branch_name still gets set (intake-supplied — the PR's own head branch)
    # even on the unbound path, since ticket creation is shared.
    assert ticket.branch_name == "feat-unbound"
    assert ticket.current_workflow_execution_id is not None
    assert ticket.current_run_id is None


@pytest.mark.service
@pytest.mark.asyncio
async def test_bound_synchronize_starts_pr_commits_run_on_same_ticket(db_session) -> None:
    org_id = await _seed_org(db_session)
    pipeline_id, _pipeline_name = await _seed_pipeline(db_session, org_id)
    for point_id in ("github:pr_opened", "github:pr_commits"):
        await add_binding(
            org_id,
            "acme/web",
            spec=TriggerBindingSpec(intake_point_id=point_id, pipeline_id=pipeline_id),
            actor=Actor.system(),
            session=db_session,
        )
    await db_session.commit()

    intake = GithubIntakeType()
    await intake._prepare_review_or_run(
        payload=_pr_payload(number=3, head_ref="feat-sync", head_sha="head1"),
        delivery=f"evt-{uuid4().hex}",
        org_id=org_id,
        session=db_session,
    )
    await db_session.commit()

    detail = await intake._handle_synchronize(
        payload=_pr_payload(number=3, head_ref="feat-sync", head_sha="head2", action="synchronize"),
        org_id=org_id,
    )
    assert detail == "pipeline_run_started"

    ticket = await _only_ticket(db_session, org_id)

    from app.core.audit_log import ActorKind  # noqa: PLC0415
    from app.core.auth import org_context  # noqa: PLC0415
    from app.domain.pipelines import list_runs_for_ticket  # noqa: PLC0415

    async with org_context(org_id, ActorKind.SYSTEM):
        runs = await list_runs_for_ticket(ticket.id, session=db_session)
    assert len(runs) == 2


@pytest.mark.service
@pytest.mark.asyncio
async def test_unbound_synchronize_refreshes_metadata_only(db_session, _stub_pr_review_engine) -> None:
    org_id = await _seed_org(db_session)

    intake = GithubIntakeType()
    await intake._prepare_review_or_run(
        payload=_pr_payload(number=4, head_ref="feat-unbound-sync", head_sha="head1"),
        delivery=f"evt-{uuid4().hex}",
        org_id=org_id,
        session=db_session,
    )
    await db_session.commit()

    detail = await intake._handle_synchronize(
        payload=_pr_payload(number=4, head_ref="feat-unbound-sync", head_sha="head2", action="synchronize"),
        org_id=org_id,
    )
    assert detail == "synchronize"

    ticket = await _only_ticket(db_session, org_id)
    assert ticket.current_run_id is None


@pytest.mark.service
@pytest.mark.asyncio
async def test_pipeline_start_failure_surfaces_as_audit_row_not_error(db_session) -> None:
    """A binding whose target pipeline can't be resolved (`PipelineNotFoundError`
    — the same "config problem, not delivery problem" class as
    `PipelineValidationError`) never turns into a retry-worthy failure: the
    intake outcome stays 2xx-shaped and the failure is recorded as an audit
    row on the ticket."""
    org_id = await _seed_org(db_session)
    intake = GithubIntakeType()

    missing_pipeline_id = uuid4()
    bogus_binding = TriggerBinding(
        id=uuid4(),
        repo_external_id="acme/web",
        intake_point_id="github:pr_opened",
        pipeline_id=missing_pipeline_id,
        pipeline_name="ghost",
        schedule=None,
    )

    outcome = await intake._prepare_pipeline_runs(
        bindings=[bogus_binding],
        payload=_pr_payload(number=5, head_ref="feat-broken-binding", head_sha="eee"),
        delivery=f"evt-{uuid4().hex}",
        org_id=org_id,
        session=db_session,
    )
    await db_session.commit()

    assert outcome.detail == "pipeline_start_failed"

    ticket = await _only_ticket(db_session, org_id)
    assert ticket.current_run_id is None

    entries = await list_audit_for_entity("ticket", ticket.id, org_id=org_id)
    kinds = [e.kind for e in entries]
    assert "ticket.pipeline_start_failed" in kinds
