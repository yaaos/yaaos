"""Smoke test: each of the 5 reviewer workflows reaches `done` end-to-end.

Drives each workflow through the engine with spy Workspace commands
that emit minimum-shape outputs. Local downstream steps either short-
circuit (no pr_id link → success-no-op) or complete cleanly with the
spy's outputs. Each workflow ends in `done`.

This is the composition story for slice 23 generalized to all 5
workflows. Per-workflow side-effect verification (e.g. ResolveFinding
actually transitioning a finding state, ArchiveStaleFindings flipping
to STALE) is exercised in the unit + service tests for each command
body — this test verifies the wiring + workflow shape, not the bodies'
internals.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.core.outbox import drain_once
from app.core.outbox.models import OutboxEntryRow
from app.core.plugin_meta import PluginMeta
from app.core.workflow import Outcome, WorkflowExecutionRow, WorkflowState, _reset_for_tests, get_engine
from app.core.workspace import (
    WorkspaceTicketContext,
    _reset_providers_for_tests,
    _reset_workflow_context_provider_for_tests,
    register_workflow_context_provider,
    register_workspace_provider,
)
from app.domain.reviewer.commands import (
    ALL_LOCAL_COMMANDS,
    AnswerQuestion,
    CodeReview,
    IncrementalReview,
    StaleCheck,
    VerifyFix,
)
from app.domain.reviewer.workflows import (
    answer_question_v1,
    incremental_review_v1,
    stale_check_v1,
    verify_fix_v1,
)


class _StubWorkspaceProvider:
    meta = PluginMeta(id="in_process", type="workspace", display_name="stub")

    async def provision(self, spec):  # type: ignore[no-untyped-def]
        return {"sha": spec.sha}

    async def destroy(self, plugin_state):  # type: ignore[no-untyped-def]
        return None

    async def health_check(self, plugin_state):  # type: ignore[no-untyped-def]
        del plugin_state
        return None

    async def run_coding_agent_cli(self, plugin_state, argv, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def read_text(self, plugin_state, path):  # type: ignore[no-untyped-def]
        return None

    async def write_text(self, plugin_state, path, content):  # type: ignore[no-untyped-def]
        return None


class _StaticContextProvider:
    def __init__(self, context: WorkspaceTicketContext) -> None:
        self._context = context

    async def get_workspace_ticket_context(self, ticket_id):  # type: ignore[no-untyped-def]
        del ticket_id
        return self._context


async def _drain_workflow_outbox(db_session, *, max_iterations: int = 50) -> int:
    from app.core.tasks.service import get_registered  # noqa: PLC0415

    total = 0
    for _ in range(max_iterations):
        rows = (
            (
                await db_session.execute(
                    select(OutboxEntryRow)
                    .where(OutboxEntryRow.dispatched_at.is_(None))
                    .order_by(OutboxEntryRow.created_at)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return total

        async def _dispatcher(kind: str, payload: dict) -> None:
            assert kind == "taskiq_enqueue"
            body = get_registered(payload["task_name"])
            assert body is not None
            ctx = type(
                "Ctx",
                (),
                {
                    "session": None,
                    "traceparent": payload.get("args", {}).get("traceparent"),
                    "attempt": 0,
                    "job_id": "test",
                },
            )()
            await body(ctx, **payload["args"])

        delivered = await drain_once(db_session, dispatcher=_dispatcher)
        await db_session.commit()
        total += delivered
        if delivered == 0:
            break
    return total


# Spy subclasses — each emits the minimum shape the downstream Local step needs
# to short-circuit success-no-op. None of these spies do real work.


class _SpyCodeReview(CodeReview):
    async def _run_in_workspace(self, workspace, ticket_ctx, inputs, ctx):  # type: ignore[no-untyped-def]
        del workspace, ticket_ctx, inputs, ctx
        return Outcome.success(outputs={"draft_findings": []})  # PostFindings → no-op


class _SpyIncrementalReview(IncrementalReview):
    async def _run_in_workspace(self, workspace, ticket_ctx, inputs, ctx):  # type: ignore[no-untyped-def]
        del workspace, ticket_ctx, inputs, ctx
        return Outcome.success(outputs={"draft_findings": []})


class _SpyVerifyFix(VerifyFix):
    async def _run_in_workspace(self, workspace, ticket_ctx, inputs, ctx):  # type: ignore[no-untyped-def]
        del workspace, ticket_ctx, inputs, ctx
        # ResolveFinding handles None verdict → success-no-op.
        return Outcome.success(outputs={"verdict": {}})


class _SpyStaleCheck(StaleCheck):
    async def _run_in_workspace(self, workspace, ticket_ctx, inputs, ctx):  # type: ignore[no-untyped-def]
        del workspace, ticket_ctx, inputs, ctx
        # ArchiveStaleFindings with empty list → success-no-op.
        return Outcome.success(outputs={"stale_finding_ids": []})


class _SpyAnswerQuestion(AnswerQuestion):
    async def _run_in_workspace(self, workspace, ticket_ctx, inputs, ctx):  # type: ignore[no-untyped-def]
        del workspace, ticket_ctx, inputs, ctx
        # PostReply with empty reply_body → success-no-op.
        return Outcome.success(outputs={"reply_body": ""})


@pytest.fixture
def _engine_with_stubs():
    _reset_for_tests()
    _reset_providers_for_tests()
    _reset_workflow_context_provider_for_tests()
    register_workspace_provider(_StubWorkspaceProvider())
    register_workflow_context_provider(
        _StaticContextProvider(
            WorkspaceTicketContext(
                org_id=uuid4(),
                plugin_id="github",
                repo_external_id="me/repo",
                payload={"head_sha": "deadbeef", "base_sha": "babecafe"},
                pr_id=None,  # no PR link → downstream Local steps short-circuit
            )
        )
    )
    eng = get_engine()
    from app.core.workspace.commands import ALL_LIFECYCLE_COMMANDS  # noqa: PLC0415

    for cmd in ALL_LIFECYCLE_COMMANDS:
        eng.register_command(cmd)
    eng.register_command(_SpyCodeReview())
    eng.register_command(_SpyIncrementalReview())
    eng.register_command(_SpyVerifyFix())
    eng.register_command(_SpyStaleCheck())
    eng.register_command(_SpyAnswerQuestion())
    for cmd in ALL_LOCAL_COMMANDS:
        eng.register_command(cmd)
    yield eng
    _reset_for_tests()
    _reset_providers_for_tests()
    _reset_workflow_context_provider_for_tests()


_OTHER_FOUR_WORKFLOWS = [incremental_review_v1, verify_fix_v1, stale_check_v1, answer_question_v1]


@pytest.mark.parametrize("workflow", _OTHER_FOUR_WORKFLOWS, ids=lambda w: w.name)
async def test_workflow_reaches_done(db_session, _engine_with_stubs, workflow) -> None:  # type: ignore[no-untyped-def]
    """Each non-pr_review_v1 reviewer workflow walks to DONE end-to-end.
    pr_review_v1 is covered separately by test_pr_review_v1_e2e_service."""
    from app.domain.tickets import create as create_ticket  # noqa: PLC0415

    _engine_with_stubs.register_workflow(workflow)

    # CheckShouldReview (in incremental_review_v1) reads tickets.get_payload —
    # so we need a real ticket row. Other workflows don't run CheckShouldReview
    # but creating one universally keeps the test simple.
    org_id = uuid4()
    ticket_id, _ = await create_ticket(
        type="github_pr",
        payload={
            "is_draft": False,
            "is_fork": False,
            "labels": [],
            "author_login": "alice",
            "head_sha": "deadbeef",
            "base_sha": "babecafe",
        },
        idempotency_key=f"smoke-{workflow.name}-{uuid4()}",
        org_id=org_id,
        title="t",
        source="github_pr",
        source_external_id="42",
        plugin_id="github",
        repo_external_id="me/repo",
        session=db_session,
    )
    await db_session.commit()

    wfx_id = await _engine_with_stubs.start(
        workflow_name=workflow.name,
        ticket_id=str(ticket_id),
        workspace_provider="in_memory",
        ticket_payload={
            "head_sha": "deadbeef",
            "base_sha": "babecafe",
            "finding_id": str(uuid4()),
            "finding_ids": [str(uuid4())],
            "question_body": "test question",
        },
        session=db_session,
    )
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, UUID(wfx_id))
    assert wfx.state == WorkflowState.DONE.value, (
        f"workflow {workflow.name} ended in state={wfx.state!r}, expected done"
    )
    assert wfx.pending_agent_command_id is None
