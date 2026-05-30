"""End-to-end composition test for the `pr_review_v1` workflow.

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

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.core.plugin_kit import PluginMeta
from app.core.tasks import drain_once, get_pending_task_names
from app.core.workflow import Outcome, WorkflowState, get_execution_summary
from app.core.workspace import (
    WorkspaceStatus,
    WorkspaceTicketContext,
    register_workflow_context_provider,
    register_workspace_provider,
)
from app.domain.reviewer.commands import (
    ALL_LOCAL_COMMANDS,
    ALL_WORKSPACE_COMMANDS,
)
from app.domain.reviewer.workflows import pr_review_v1
from app.domain.tickets import create as create_ticket
from app.testing.fake_coding_agent import register_fake_coding_agent
from app.testing.workflow_harness import scoped_engine


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
def _registered_engine(workspace_providers_isolation, workflow_context_provider_isolation):  # type: ignore[no-untyped-def]
    from app.core.workspace import ALL_LIFECYCLE_COMMANDS  # noqa: PLC0415

    register_workspace_provider(_StubWorkspaceProvider())
    # Register lifecycle + reviewer commands (mirrors domain/reviewer bootstrap).
    with scoped_engine() as eng:
        for cmd in (*ALL_LIFECYCLE_COMMANDS, *ALL_WORKSPACE_COMMANDS, *ALL_LOCAL_COMMANDS):
            eng.register_command(cmd)
        eng.register_workflow(pr_review_v1)
        yield eng


async def _drain_workflow_outbox(db_session, *, max_iterations: int = 50) -> int:
    """Drain outbox until empty. Re-dispatches `taskiq_enqueue` rows into
    the matching task body via the broker's task registry."""
    from app.core.tasks import get_broker  # noqa: PLC0415

    async def _dispatcher(kind: str, payload: dict) -> None:
        assert kind == "taskiq_enqueue"
        decorated = get_broker().find_task(payload["task_name"])
        assert decorated is not None
        await decorated.original_func(**payload["args"])

    total = 0
    for _ in range(max_iterations):
        pending = await get_pending_task_names(db_session)
        if not pending:
            return total
        delivered = await drain_once(db_session, dispatcher=_dispatcher)
        await db_session.commit()
        total += delivered
        if delivered == 0:
            break
    return total


async def _advance_pending_agent_event(  # type: ignore[no-untyped-def]
    db_session,
    wfx_id: str,
    outputs: dict[str, object] | None = None,
    *,
    outcome_label: str = "success",
):
    """Simulate the agent's terminal event for a Workspace step on
    `remote_agent` provider. Reads the workflow's `pending_agent_command_id`,
    enqueues `handle_agent_event` with the matching id + supplied outputs,
    then drains the outbox so the workflow advances to the next step (or
    DONE).
    """
    from app.core.tasks import enqueue  # noqa: PLC0415
    from app.core.workflow import HANDLE_AGENT_EVENT  # noqa: PLC0415

    wfx = await get_execution_summary(UUID(wfx_id), session=db_session)
    assert wfx is not None
    assert wfx.state == WorkflowState.AWAITING_AGENT.value, (
        f"expected AWAITING_AGENT before agent event, got {wfx.state!r}"
    )
    assert wfx.pending_agent_command_id is not None
    await enqueue(
        HANDLE_AGENT_EVENT,
        args={
            "workflow_execution_id": wfx_id,
            "agent_command_id": str(wfx.pending_agent_command_id),
            "outcome_label": outcome_label,
            "outputs": outputs or {},
            "traceparent": None,
        },
        session=db_session,
    )
    await db_session.commit()
    await _drain_workflow_outbox(db_session)


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
    #    engine routes Workspace commands inline. Register a fake
    #    coding_agent so CodeReview's real body has a plugin
    #    to call. Fake returns 0 findings → PostFindings is a no-op.
    with register_fake_coding_agent():
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
    wfx = await get_execution_summary(UUID(wfx_id), session=db_session)
    assert wfx.state == WorkflowState.DONE.value
    assert wfx.pending_agent_command_id is None

    # 6. The workspace ProvisionWorkspace created should be flipped to expired
    #    by CleanupWorkspace.
    from sqlalchemy import text  # noqa: PLC0415

    ws_statuses = (
        await db_session.execute(text("SELECT status FROM workspaces WHERE org_id = :oid"), {"oid": org_id})
    ).all()
    assert len(ws_statuses) == 1, "expected exactly one workspace row for this org"
    assert ws_statuses[0][0] == WorkspaceStatus.EXPIRED.value


async def test_pr_review_v1_with_findings_persists_to_db(
    db_session, workspace_providers_isolation, workflow_context_provider_isolation
) -> None:  # type: ignore[no-untyped-def]
    """Full workflow walk with a spy CodeReview that emits realistic
    FindingDrafts. Verifies the entire pipeline composes:
    intake → CheckShouldReview → ProvisionWorkspace → CodeReview (spy) →
    PostFindings (real body) → admission → CleanupWorkspace. After the
    workflow ends DONE, FindingRow rows exist for the PR.
    """
    from app.domain.pull_requests import upsert as upsert_pr  # noqa: PLC0415
    from app.domain.reviewer.commands import CodeReview  # noqa: PLC0415
    from app.domain.reviewer.models import FindingRow  # noqa: PLC0415
    from app.domain.tickets import create as create_ticket2  # noqa: PLC0415
    from app.domain.vcs import VCSPullRequest as _VCSPullRequest  # noqa: PLC0415

    # 1. Workspace provider whose plugin_state carries the file content the
    #    finding's anchor references. CodeReview spy never reads it (it just
    #    emits the dicts); PostFindings DOES via the workspace.read_text path.
    class _StubProviderWithFiles:
        meta = PluginMeta(id="in_process", type="workspace", display_name="stub")

        async def provision(self, spec):  # type: ignore[no-untyped-def]
            return {
                "sha": spec.sha,
                "files": {"src/foo.py": "def foo(x):\n    return x.value\n"},
            }

        async def destroy(self, plugin_state):  # type: ignore[no-untyped-def]
            return None

        async def health_check(self, plugin_state):  # type: ignore[no-untyped-def]
            del plugin_state
            return None

        async def run_coding_agent_cli(self, plugin_state, argv, **kwargs):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        async def read_text(self, plugin_state, path):  # type: ignore[no-untyped-def]
            return plugin_state.get("files", {}).get(path)

        async def write_text(self, plugin_state, path, content):  # type: ignore[no-untyped-def]
            return None

    register_workspace_provider(_StubProviderWithFiles())

    org_id = uuid4()

    # 2. Real ticket + PR rows so admission's findings FK lands cleanly.
    ext_id = f"e2e-{uuid4().hex[:6]}"
    ticket_id, _ = await create_ticket2(
        type="pr_review",
        payload={
            "is_draft": False,
            "is_fork": False,
            "labels": [],
            "author_login": "alice",
            "head_sha": "deadbeef",
            "base_sha": "babecafe",
        },
        idempotency_key=ext_id,
        org_id=org_id,
        title="t",
        source="github_pr",
        source_external_id=ext_id,
        plugin_id="github",
        repo_external_id="me/repo",
        session=db_session,
    )
    pr = await upsert_pr(
        _VCSPullRequest(
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
    pr_id = pr.id
    await db_session.commit()

    # 3. Context provider returns the real ticket_id-linked PR.
    register_workflow_context_provider(
        _StaticWorkflowContextProvider(
            WorkspaceTicketContext(
                org_id=org_id,
                plugin_id="github",
                repo_external_id="me/repo",
                payload={"head_sha": "deadbeef", "base_sha": "babecafe"},
                pr_id=pr_id,
            )
        )
    )

    # 4. Spy CodeReview emits one FindingDraft. The real PostFindings will
    #    deserialize it, read the file, build the RawFinding, admit, persist.
    spy_finding = {
        "severity": "major",
        "rule_id": "spy_rule",
        "title": "Spy finding",
        "body": "Spy body.",
        "concrete_failure_scenario": (
            "Caller can pass None; foo() dereferences without a check; raises NoneType."
        ),
        "confidence": 90,
        "rationale": "Function signature accepts any.",
        "anchor": {"file_path": "src/foo.py", "line_start": 2, "line_end": 2},
    }

    class _SpyCodeReview(CodeReview):
        async def _run_in_workspace(self, workspace, ticket_ctx, inputs, ctx):  # type: ignore[no-untyped-def]
            del workspace, ticket_ctx, inputs, ctx
            return Outcome.success(outputs={"draft_findings": [spy_finding]})

    # 5. Register the workflow + commands with the spy override.
    from app.core.workspace import ALL_LIFECYCLE_COMMANDS  # noqa: PLC0415
    from app.testing.stub_vcs import register_stub_vcs  # noqa: PLC0415

    with scoped_engine() as eng:
        for cmd in ALL_LIFECYCLE_COMMANDS:
            eng.register_command(cmd)
        eng.register_command(_SpyCodeReview())  # overrides stub CodeReview
        for cmd in ALL_LOCAL_COMMANDS:
            eng.register_command(cmd)
        # Skip the rest of ALL_WORKSPACE_COMMANDS (CodeReview replaced; others
        # not referenced in pr_review_v1).
        eng.register_workflow(pr_review_v1)

        # 6. Kick off + drain.
        with register_stub_vcs(plugin_id="github"):
            wfx_id = await eng.start(
                workflow_name="pr_review_v1",
                ticket_id=str(ticket_id),
                workspace_provider="in_memory",
                ticket_payload={
                    "head_sha": "deadbeef",
                    "base_sha": "babecafe",
                    "is_draft": False,
                    "is_fork": False,
                },
                session=db_session,
            )
            await db_session.commit()
            await _drain_workflow_outbox(db_session)

        wfx = await get_execution_summary(UUID(wfx_id), session=db_session)
        assert wfx.state == WorkflowState.DONE.value

        # 7. FindingRow lands.
        rows = (
            (
                await db_session.execute(
                    select(FindingRow).where(FindingRow.pr_id == pr_id, FindingRow.org_id == org_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].rule_id == "spy_rule"
        assert rows[0].title == "Spy finding"


async def test_pr_review_v1_runs_end_to_end_remote_agent(db_session, _registered_engine) -> None:  # type: ignore[no-untyped-def]
    """Provider parity: the same `pr_review_v1` workflow walks to DONE
    against `workspace_provider="remote_agent"` exactly as it does under
    `in_memory` — only the dispatch path differs. Workspace-category
    commands (`ProvisionWorkspace`, `CodeReview`, `CleanupWorkspace`)
    land at AWAITING_AGENT instead of running inline; the test simulates
    each terminal AgentEvent via `_advance_pending_agent_event`. Local
    commands (`CheckShouldReview`, `PostFindings`) still execute inline
    on the control plane — proves the engine treats provider as a
    dispatch concern, not a workflow-shape concern.

    The remote-dispatch path is a synthesize-command-id stub
    (`workflow.start_step.workspace_remote_dispatch_stub`) and the test
    fills in for the agent, giving provider-parity audit coverage.
    """
    org_id = uuid4()
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
        idempotency_key=f"e2e-remote-{uuid4()}",
        org_id=org_id,
        title="real-ticket",
        source="github_pr",
        source_external_id="42",
        plugin_id="github",
        repo_external_id="me/repo",
        session=db_session,
    )
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

    wfx_id = await _registered_engine.start(
        workflow_name="pr_review_v1",
        ticket_id=str(ticket_id),
        workspace_provider="remote_agent",
        session=db_session,
    )
    await db_session.commit()

    # Initial drain — CheckShouldReview (Local) executes inline; then
    # ProvisionWorkspace (Workspace) hits the remote-dispatch stub and
    # parks at AWAITING_AGENT. drain returns when no more outbox rows.
    await _drain_workflow_outbox(db_session)

    # Simulate agent CreateWorkspace.result with a synthetic workspace_id.
    sim_workspace_id = str(uuid4())
    await _advance_pending_agent_event(db_session, wfx_id, outputs={"workspace_id": sim_workspace_id})

    # Now parked at AWAITING_AGENT on CodeReview. Simulate the
    # InvokeClaudeCode.result event with no draft findings.
    await _advance_pending_agent_event(
        db_session,
        wfx_id,
        outputs={"draft_findings": [], "summary_body": "", "state": "COMMENT"},
    )

    # PostFindings (Local) ran inline with empty drafts → success-no-op;
    # CleanupWorkspace then parked. Simulate its terminal event.
    await _advance_pending_agent_event(db_session, wfx_id, outputs={})

    # Workflow terminal — done.
    wfx = await get_execution_summary(UUID(wfx_id), session=db_session)
    assert wfx.state == WorkflowState.DONE.value
    assert wfx.pending_agent_command_id is None
