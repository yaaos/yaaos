"""End-to-end composition test for the four shipped Phase 4 slices.

Drives a ticket through `pr_review_v1` with the in_memory workspace
provider. Asserts:

- CheckShouldReview (real body) reads admission signals from ticket
  payload; non-draft non-fork PR advances past the skip gate.
- ProvisionWorkspace (real body) fetches ticket context through the
  registered WorkflowContextProvider and creates a WorkspaceRow via the
  stub workspace provider.
- CodeReview + PostFindings (stub bodies returning Outcome.success()) let
  the workflow advance.
- CleanupWorkspace (real body) flips the WorkspaceRow to expired.

Workflow terminates in `done` state; workspace row is `expired`. This is
the composition proof that the dispatch routing + provider callback +
two real lifecycle bodies actually fit together.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.core.outbox import drain_once
from app.core.outbox.models import OutboxEntryRow
from app.core.plugin_meta import PluginMeta
from app.core.workflow import WorkflowExecutionRow, WorkflowState, _reset_for_tests, get_engine
from app.core.workspace import (
    WorkspaceTicketContext,
    _reset_providers_for_tests,
    _reset_workflow_context_provider_for_tests,
    register_workflow_context_provider,
    register_workspace_provider,
)
from app.core.workspace.models import WorkspaceRow
from app.core.workspace.types import WorkspaceStatus
from app.domain.reviewer.commands import (
    ALL_LOCAL_COMMANDS,
    ALL_WORKSPACE_COMMANDS,
)
from app.domain.reviewer.workflows import pr_review_v1
from app.domain.tickets import create as create_ticket


class _StubWorkspaceProvider:
    """Doesn't clone anything — just returns a fake plugin_state so
    create_workspace() lands a real row in the DB."""

    meta = PluginMeta(id="in_process", type="workspace", display_name="stub-in-memory")

    async def provision(self, spec):  # type: ignore[no-untyped-def]
        return {"working_dir": "/tmp/stub", "sha": spec.sha}

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


class _StaticWorkflowContextProvider:
    """Returns a fixed WorkspaceTicketContext regardless of ticket_id —
    matches the single-ticket scope of this test."""

    def __init__(self, context: WorkspaceTicketContext) -> None:
        self._context = context

    async def get_workspace_ticket_context(self, ticket_id):  # type: ignore[no-untyped-def]
        del ticket_id
        return self._context


@pytest.fixture
def _registered_engine():
    _reset_for_tests()
    _reset_providers_for_tests()
    _reset_workflow_context_provider_for_tests()

    register_workspace_provider(_StubWorkspaceProvider())
    eng = get_engine()
    # Register lifecycle + reviewer commands (mirrors domain/reviewer bootstrap).
    from app.core.workspace.commands import ALL_LIFECYCLE_COMMANDS  # noqa: PLC0415

    for cmd in (*ALL_LIFECYCLE_COMMANDS, *ALL_WORKSPACE_COMMANDS, *ALL_LOCAL_COMMANDS):
        eng.register_command(cmd)
    eng.register_workflow(pr_review_v1)
    yield eng
    _reset_for_tests()
    _reset_providers_for_tests()
    _reset_workflow_context_provider_for_tests()


async def _drain_workflow_outbox(db_session, *, max_iterations: int = 50) -> int:
    """Drain outbox until empty. Re-dispatches `taskiq_enqueue` rows into
    the matching task body via `core/tasks.get_registered`."""
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


async def test_pr_review_v1_runs_end_to_end_in_memory(db_session, _registered_engine) -> None:  # type: ignore[no-untyped-def]
    org_id = uuid4()
    # 1. Create a ticket the way intake would.
    ticket_id, _ = await create_ticket(
        type="github_pr",
        payload={
            "is_draft": False,
            "is_fork": False,
            "labels": ["enhancement"],
            "author_login": "alice",
            "pr_external_id": "42",
            "head_sha": "deadbeefcafef00d",
            "base_sha": "babecafe",
        },
        idempotency_key=f"e2e-{uuid4()}",
        org_id=org_id,
        title="real-ticket",
        source="github_pr",
        source_external_id="42",
        plugin_id="github",
        repo_external_id="me/repo",
        session=db_session,
    )
    # 2. Wire the workflow-context provider for this org's ticket.
    register_workflow_context_provider(
        _StaticWorkflowContextProvider(
            WorkspaceTicketContext(
                org_id=org_id,
                plugin_id="github",
                repo_external_id="me/repo",
                payload={"head_sha": "deadbeefcafef00d", "base_sha": "babecafe"},
            )
        )
    )
    # 3. Kick off pr_review_v1 with workspace_provider=in_memory so the
    #    engine routes Workspace commands inline.
    wfx_id = await _registered_engine.start(
        workflow_name="pr_review_v1",
        ticket_id=str(ticket_id),
        workspace_provider="in_memory",
        session=db_session,
    )
    await db_session.commit()

    # 4. Drain the outbox; each iteration of start_step + route_workflow
    #    delivers via the dispatcher.
    await _drain_workflow_outbox(db_session)

    # 5. Workflow terminal — done.
    wfx = await db_session.get(WorkflowExecutionRow, UUID(wfx_id))
    assert wfx.state == WorkflowState.DONE.value
    assert wfx.pending_agent_command_id is None

    # 6. The workspace ProvisionWorkspace created should be flipped to expired
    #    by CleanupWorkspace.
    rows = (
        (await db_session.execute(select(WorkspaceRow).where(WorkspaceRow.org_id == org_id))).scalars().all()
    )
    assert len(rows) == 1, "expected exactly one workspace row for this org"
    assert rows[0].status == WorkspaceStatus.EXPIRED.value
