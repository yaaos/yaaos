"""SecretsScan Local WorkflowCommand — pre-flight secrets gate.

Verifies the workflow-engine equivalent of the legacy_runner.py
secrets detection: when the PR diff contains a known secret pattern,
SecretsScan posts a warning Review via the VCS plugin and returns
`Outcome.success(label="skip", outputs={"reason": "secrets_detected"})`
so the workflow's `skip` transition terminates the run.

Closes the feature-parity gap that was blocking queue.py file deletion
(legacy `_run_review_job_inner` owns secrets detection today).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.core.workflow import CommandContext
from app.core.workspace import (
    WorkspaceTicketContext,
    clear_workflow_context_provider,
    register_workflow_context_provider,
)
from app.domain.reviewer.commands import SecretsScan
from app.domain.vcs import Diff
from app.testing.stub_vcs import register_stub_vcs


class _StaticCtxProvider:
    def __init__(self, ctx: WorkspaceTicketContext | None) -> None:
        self._ctx = ctx

    async def get_workspace_ticket_context(self, ticket_id):  # type: ignore[no-untyped-def]
        del ticket_id
        return self._ctx


def _cmd_ctx() -> CommandContext:
    return CommandContext(
        workflow_execution_id=str(uuid4()),
        ticket_id=str(uuid4()),
        step_id="secrets",
        attempt=0,
    )


@pytest.fixture(autouse=True)
def _reset_context():
    yield
    clear_workflow_context_provider()


async def test_secrets_scan_skips_when_diff_contains_aws_key() -> None:
    """A `+`-prefixed line with an AWS access-key pattern triggers
    `Outcome.success(label="skip", outputs.reason="secrets_detected")`
    and posts the warning Review via the VCS plugin."""
    pr_external_id = "pr-123"
    leaked = "+AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'\n"
    with register_stub_vcs(plugin_id="github") as stub:
        stub.set_diff(pr_external_id, Diff(raw=leaked, files=[]))
        register_workflow_context_provider(
            _StaticCtxProvider(
                WorkspaceTicketContext(
                    org_id=uuid4(),
                    plugin_id="github",
                    repo_external_id="me/repo",
                    payload={"pr_external_id": pr_external_id},
                    pr_id=uuid4(),
                )
            )
        )

        outcome = await SecretsScan().execute({}, _cmd_ctx())

    assert outcome.label == "skip"
    assert outcome.outputs["reason"] == "secrets_detected"
    assert outcome.outputs["rule_id"] == "aws_access_key"
    # Warning Review posted in-band so the human sees yaaos's refusal.
    assert len(stub.posted_reviews) == 1
    _, review = stub.posted_reviews[0]
    assert review.state == "COMMENT"
    assert "aws_access_key" in (review.summary_body or "")


async def test_secrets_scan_advances_when_diff_is_clean() -> None:
    """A clean diff returns `Outcome.success` with rule_id=None — no
    `skip` label, so the workflow advances to ProvisionWorkspace."""
    pr_external_id = "pr-clean"
    with register_stub_vcs(plugin_id="github") as stub:
        stub.set_diff(pr_external_id, Diff(raw="+def foo(): return 42\n", files=[]))
        register_workflow_context_provider(
            _StaticCtxProvider(
                WorkspaceTicketContext(
                    org_id=uuid4(),
                    plugin_id="github",
                    repo_external_id="me/repo",
                    payload={"pr_external_id": pr_external_id},
                    pr_id=uuid4(),
                )
            )
        )

        outcome = await SecretsScan().execute({}, _cmd_ctx())

    assert outcome.label == "success"
    assert outcome.outputs.get("rule_id") is None
    # No warning posted on a clean diff.
    assert stub.posted_reviews == []


async def test_secrets_scan_advances_when_no_pr_link() -> None:
    """Workflows whose ticket has no `pr_id` skip the gate as a no-op —
    upstream `CheckShouldReview` already handled ticket-payload signals."""
    register_workflow_context_provider(
        _StaticCtxProvider(
            WorkspaceTicketContext(
                org_id=uuid4(),
                plugin_id="github",
                repo_external_id="me/repo",
                payload={},
                pr_id=None,
            )
        )
    )

    outcome = await SecretsScan().execute({}, _cmd_ctx())

    assert outcome.label == "success"
    assert outcome.outputs.get("rule_id") is None


async def test_secrets_scan_advances_when_diff_fetch_fails() -> None:
    """Diff-fetch failures are best-effort — log + advance. We don't
    want a transient VCS hiccup to block reviews."""

    class _RaisingPlugin:
        from app.core.plugin_kit import PluginMeta  # noqa: PLC0415

        meta = PluginMeta(id="github", type="vcs", display_name="raises")

        async def fetch_diff(self, external_id):  # type: ignore[no-untyped-def]
            raise RuntimeError("github transient")

    from app.domain.vcs import scoped_vcs_plugin  # noqa: PLC0415

    with scoped_vcs_plugin(_RaisingPlugin()):  # type: ignore[arg-type]
        register_workflow_context_provider(
            _StaticCtxProvider(
                WorkspaceTicketContext(
                    org_id=uuid4(),
                    plugin_id="github",
                    repo_external_id="me/repo",
                    payload={"pr_external_id": "pr-x"},
                    pr_id=uuid4(),
                )
            )
        )

        outcome = await SecretsScan().execute({}, _cmd_ctx())

        assert outcome.label == "success"
        assert outcome.outputs.get("rule_id") is None
