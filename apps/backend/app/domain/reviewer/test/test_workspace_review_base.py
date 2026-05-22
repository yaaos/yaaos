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

from app.core.plugin_meta import PluginMeta
from app.core.workflow import CommandContext, Outcome
from app.core.workspace import (
    Workspace,
    _reset_providers_for_tests,
    register_workspace_provider,
)
from app.core.workspace.models import WorkspaceRow
from app.core.workspace.types import WorkspaceStatus
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


@pytest.fixture
def _stub_provider():
    _reset_providers_for_tests()
    register_workspace_provider(_StubProvider())
    yield
    _reset_providers_for_tests()


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

    captured: dict[str, Workspace] = {}

    class _CodeReviewSpy(CodeReview):
        async def _run_in_workspace(self, workspace, inputs, ctx):  # type: ignore[no-untyped-def]
            captured["ws"] = workspace
            captured["inputs"] = inputs
            return Outcome.success(outputs={"draft_findings": []})

    outcome = await _CodeReviewSpy().execute({"workspace_id": str(ws_id)}, _ctx())
    assert outcome.label == "success"
    assert captured["ws"].id == str(ws_id)
    assert captured["inputs"]["workspace_id"] == str(ws_id)
