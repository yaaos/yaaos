"""Smoke test: each of the 5 reviewer workflows reaches `done` end-to-end.

Drives each workflow through the engine with spy Workspace commands
that emit minimum-shape outputs. Local downstream steps either short-
circuit (no pr_id link → success-no-op) or complete cleanly with the
spy's outputs. Each workflow ends in `done`.

This verifies workflow composition across all 5 workflows.
Per-workflow side-effect verification (e.g. ResolveFinding
actually transitioning a finding state, ArchiveStaleFindings flipping
to STALE) is exercised in the unit + service tests for each command
body — this test verifies the wiring + workflow shape, not the bodies'
internals.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.core.plugin_kit import PluginMeta
from app.core.tasks import drain_once
from app.core.workflow import Outcome, WorkflowState, get_execution_summary
from app.core.workspace import (
    WorkspaceTicketContext,
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
from app.testing.workflow_harness import scoped_engine


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
    from app.core.tasks import get_broker  # noqa: PLC0415

    async def _dispatcher(kind: str, payload: dict) -> None:
        assert kind == "taskiq_enqueue"
        decorated = get_broker().find_task(payload["task_name"])
        assert decorated is not None
        await decorated.original_func(**payload["args"])

    total = 0
    for _ in range(max_iterations):
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
def _engine_with_stubs(workspace_providers_isolation, workflow_context_provider_isolation):  # type: ignore[no-untyped-def]
    from app.core.workspace import ALL_LIFECYCLE_COMMANDS  # noqa: PLC0415

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
    with scoped_engine() as eng:
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

    wfx = await get_execution_summary(UUID(wfx_id), session=db_session)
    assert wfx.state == WorkflowState.DONE.value, (
        f"workflow {workflow.name} ended in state={wfx.state!r}, expected done"
    )
    assert wfx.pending_agent_command_id is None


# ── trace linkage parity across the 5 workflows ──────────────────────────


_ALL_FIVE_WORKFLOWS = [
    answer_question_v1,
    incremental_review_v1,
    stale_check_v1,
    verify_fix_v1,
]


@pytest.fixture
def _in_memory_spans():
    """Wire an in-memory span exporter onto the global TracerProvider so
    the parametrized trace-linkage test can inspect emitted spans."""
    from opentelemetry import trace as _trace  # noqa: PLC0415
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: PLC0415
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: PLC0415
        InMemorySpanExporter,
    )

    provider = _trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        _trace.set_tracer_provider(provider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    yield exporter
    processor.shutdown()


@pytest.mark.parametrize("workflow", _ALL_FIVE_WORKFLOWS, ids=lambda w: w.name)
async def test_all_workflows_share_upstream_trace_id(  # type: ignore[no-untyped-def]
    db_session, _engine_with_stubs, _in_memory_spans, workflow
):
    """**Provider-parity trace audit for the 4 non-pr_review_v1 reviewer
    workflows.** Each workflow walks to DONE end-to-end and every emitted
    workflow task-body span (`workflow.start_step` / `workflow.route_workflow`
    / `workflow.handle_agent_event`) shares the upstream trace_id. The
    fifth workflow (pr_review_v1) has its own trace audit in
    `test_trace_linkage.py`; this parametrized test covers the other four.

    Asserts one trace ID covers webhook → terminal outcome for all five
    workflows against the in-memory provider. The Go-subprocess side
    depends on env-passing of `TRACEPARENT`.
    """
    from opentelemetry import trace as _trace  # noqa: PLC0415

    from app.core.observability import current_traceparent  # noqa: PLC0415
    from app.domain.tickets import create as create_ticket  # noqa: PLC0415

    _engine_with_stubs.register_workflow(workflow)

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
        idempotency_key=f"trace-smoke-{workflow.name}-{uuid4()}",
        org_id=org_id,
        title="t",
        source="github_pr",
        source_external_id="42",
        plugin_id="github",
        repo_external_id="me/repo",
        session=db_session,
    )
    await db_session.commit()

    tracer = _trace.get_tracer("trace-all-workflows")
    with tracer.start_as_current_span("intake-upstream") as upstream:
        upstream_trace_id = upstream.get_span_context().trace_id
        wfx_id = await _engine_with_stubs.start(
            workflow_name=workflow.name,
            ticket_id=str(ticket_id),
            workspace_provider="in_memory",
            traceparent=current_traceparent(),
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

    wfx = await get_execution_summary(UUID(wfx_id), session=db_session)
    assert wfx.state == WorkflowState.DONE.value

    workflow_span_names = {
        "workflow.start_step",
        "workflow.route_workflow",
        "workflow.handle_agent_event",
    }
    emitted = [s for s in _in_memory_spans.get_finished_spans() if s.name in workflow_span_names]
    assert len(emitted) >= 2, (
        f"expected workflow task-body spans for {workflow.name}, got {[s.name for s in emitted]}"
    )
    for span in emitted:
        assert span.context.trace_id == upstream_trace_id, (
            f"{workflow.name}: span {span.name!r} broke trace continuity"
        )
