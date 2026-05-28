"""Verifies the `_WorkspaceReviewCommand` base contract:

- missing `workspace_id` → failure
- invalid `workspace_id` → failure
- unresolvable workspace (row missing / plugin_state unset / provider not
  registered) → failure
- happy path resolves the live `Workspace` handle and forwards it to the
  subclass's `_run_in_workspace`

The five concrete subclasses (CodeReview, IncrementalReview, VerifyFix,
StaleCheck, AnswerQuestion) inherit this contract verbatim; their
specific bodies land incrementally as `<Foo>Context` builders are
extracted from queue.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.plugin_kit import PluginMeta
from app.core.workflow import CommandContext, Outcome
from app.core.workspace import (
    WorkspaceRow,
    WorkspaceStatus,
    WorkspaceTicketContext,
    clear_workflow_context_provider,
    clear_workspace_providers,
    register_workflow_context_provider,
    register_workspace_provider,
)
from app.domain.reviewer.commands import CodeReview


class _StubProvider:
    meta = PluginMeta(id="in_process", type="workspace", display_name="stub")

    async def provision(self, spec):  # type: ignore[no-untyped-def]
        return {"sha": spec.sha}

    async def destroy(self, plugin_state):  # type: ignore[no-untyped-def]
        return None

    async def health_check(self, plugin_state):  # type: ignore[no-untyped-def]
        del plugin_state
        return None

    async def run_coding_agent_cli(self, plugin_state, argv, **kwargs):  # type: ignore[no-untyped-def]
        del plugin_state, argv, kwargs
        return None

    async def read_text(self, plugin_state, path):  # type: ignore[no-untyped-def]
        return None

    async def write_text(self, plugin_state, path, content):  # type: ignore[no-untyped-def]
        return None


class _StaticContextProvider:
    """Returns a fixed WorkspaceTicketContext for the base's ticket_ctx
    fetch. Tests that don't care about ticket fields can omit the
    registration; the unresolvable / missing-provider paths cover those."""

    def __init__(self, context: WorkspaceTicketContext | None) -> None:
        self._context = context

    async def get_workspace_ticket_context(self, ticket_id):  # type: ignore[no-untyped-def]
        del ticket_id
        return self._context


def _default_ticket_ctx() -> WorkspaceTicketContext:
    return WorkspaceTicketContext(
        org_id=uuid4(),
        plugin_id="github",
        repo_external_id="me/repo",
        payload={},
        pr_id=None,
    )


@pytest.fixture
def _stub_provider():
    clear_workspace_providers()
    clear_workflow_context_provider()
    register_workspace_provider(_StubProvider())
    register_workflow_context_provider(_StaticContextProvider(_default_ticket_ctx()))
    yield
    clear_workspace_providers()
    clear_workflow_context_provider()


def _ctx() -> CommandContext:
    return CommandContext(
        workflow_execution_id=str(uuid4()),
        ticket_id=str(uuid4()),
        step_id="review",
        attempt=0,
    )


async def test_missing_workspace_id_returns_failure() -> None:
    outcome = await CodeReview().execute({}, _ctx())
    assert outcome.label == "failure"
    assert "missing workspace_id" in (outcome.failure_reason or "")


async def test_invalid_workspace_id_returns_failure() -> None:
    outcome = await CodeReview().execute({"workspace_id": "not-a-uuid"}, _ctx())
    assert outcome.label == "failure"
    assert "invalid workspace_id" in (outcome.failure_reason or "")


async def test_unresolvable_workspace_returns_failure(db_session, _stub_provider) -> None:  # type: ignore[no-untyped-def]
    """workspace_id valid + parsable but the row doesn't exist."""
    _ = db_session
    outcome = await CodeReview().execute({"workspace_id": str(uuid4())}, _ctx())
    assert outcome.label == "failure"
    assert "not resolvable" in (outcome.failure_reason or "")


async def test_happy_path_forwards_workspace_to_subclass(db_session, _stub_provider) -> None:  # type: ignore[no-untyped-def]
    """When the workspace resolves, the subclass's `_run_in_workspace`
    receives the live handle. Until each real body lands, the default
    body returns Outcome.success."""
    ws_id = uuid4()
    db_session.add(
        WorkspaceRow(
            id=ws_id,
            org_id=uuid4(),
            provider_id="in_process",
            spec={"sha": "feed"},
            status=WorkspaceStatus.ACTIVE.value,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            plugin_state={"sha": "feed", "working_dir": "/tmp/x"},
        )
    )
    await db_session.commit()

    captured: dict[str, object] = {}

    class _CodeReviewSpy(CodeReview):
        async def _run_in_workspace(self, workspace, ticket_ctx, inputs, ctx):  # type: ignore[no-untyped-def]
            captured["ws"] = workspace
            captured["ticket_ctx"] = ticket_ctx
            captured["inputs"] = inputs
            return Outcome.success(outputs={"draft_findings": []})

    outcome = await _CodeReviewSpy().execute({"workspace_id": str(ws_id)}, _ctx())
    assert outcome.label == "success"
    assert captured["ws"].id == str(ws_id)  # type: ignore[union-attr]
    assert isinstance(captured["ticket_ctx"], WorkspaceTicketContext)
    assert captured["inputs"]["workspace_id"] == str(ws_id)  # type: ignore[index]


async def test_no_context_provider_returns_failure(db_session) -> None:  # type: ignore[no-untyped-def]
    """Workspace resolves but no WorkflowContextProvider is registered →
    Outcome.failure. Domain bootstrap is expected to install the provider;
    a missing one is a deployment misconfig, not a workflow input error."""
    clear_workspace_providers()
    clear_workflow_context_provider()
    register_workspace_provider(_StubProvider())
    ws_id = uuid4()
    db_session.add(
        WorkspaceRow(
            id=ws_id,
            org_id=uuid4(),
            provider_id="in_process",
            spec={"sha": "x"},
            status=WorkspaceStatus.ACTIVE.value,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            plugin_state={"sha": "x"},
        )
    )
    await db_session.commit()
    try:
        outcome = await CodeReview().execute({"workspace_id": str(ws_id)}, _ctx())
        assert outcome.label == "failure"
        assert "no workflow_context provider" in (outcome.failure_reason or "")
    finally:
        clear_workspace_providers()


async def test_ticket_not_found_returns_failure(db_session) -> None:  # type: ignore[no-untyped-def]
    """Workspace + provider both resolve but provider returns None for the
    ticket → Outcome.failure. The workflow can't proceed without ticket
    context."""
    clear_workspace_providers()
    clear_workflow_context_provider()
    register_workspace_provider(_StubProvider())
    register_workflow_context_provider(_StaticContextProvider(context=None))
    ws_id = uuid4()
    db_session.add(
        WorkspaceRow(
            id=ws_id,
            org_id=uuid4(),
            provider_id="in_process",
            spec={"sha": "x"},
            status=WorkspaceStatus.ACTIVE.value,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            plugin_state={"sha": "x"},
        )
    )
    await db_session.commit()
    try:
        outcome = await CodeReview().execute({"workspace_id": str(ws_id)}, _ctx())
        assert outcome.label == "failure"
        assert "not found" in (outcome.failure_reason or "")
    finally:
        clear_workspace_providers()
        clear_workflow_context_provider()


# ── Subclass contract — applies to all 5 Workspace reviewer commands ────


from app.domain.reviewer.commands import (  # noqa: E402
    AnswerQuestion,
    IncrementalReview,
    StaleCheck,
    VerifyFix,
)

_ALL_WORKSPACE_REVIEWER_CMDS = [CodeReview, IncrementalReview, VerifyFix, StaleCheck, AnswerQuestion]


@pytest.mark.parametrize("cmd_cls", _ALL_WORKSPACE_REVIEWER_CMDS)
async def test_all_workspace_reviewers_fail_on_missing_workspace_id(cmd_cls) -> None:
    """Every Workspace reviewer command inherits the workspace_id-required
    contract from `_WorkspaceReviewCommand`. Regression guard: if someone
    overrides `execute()` on a subclass and forgets to call super(), this
    catches it."""
    outcome = await cmd_cls().execute({}, _ctx())
    assert outcome.label == "failure", f"{cmd_cls.__name__} accepted missing workspace_id"
    assert "missing workspace_id" in (outcome.failure_reason or "")


@pytest.mark.parametrize("cmd_cls", _ALL_WORKSPACE_REVIEWER_CMDS)
async def test_all_workspace_reviewers_fail_on_invalid_workspace_id(cmd_cls) -> None:
    outcome = await cmd_cls().execute({"workspace_id": "not-a-uuid"}, _ctx())
    assert outcome.label == "failure", f"{cmd_cls.__name__} accepted invalid workspace_id"
    assert "invalid workspace_id" in (outcome.failure_reason or "")
