"""`PostFindings` happy-path — drafts flow through the full admission
pipeline and admitted findings land as FindingRow rows.

Proves the wrapper drives `findingdrafts_to_raw` → `admit_raw_findings`
end-to-end with realistic inputs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.plugin_kit import PluginMeta
from app.core.workflow import CommandContext
from app.core.workspace import (
    WorkspaceRow,
    WorkspaceStatus,
    WorkspaceTicketContext,
    clear_workflow_context_provider,
    clear_workspace_providers,
    register_workflow_context_provider,
    register_workspace_provider,
)
from app.domain.pull_requests import PullRequestRow
from app.domain.reviewer.commands import PostFindings
from app.domain.reviewer.models import FindingRow
from app.domain.tickets import TicketRow


class _StubWorkspaceProvider:
    """Returns deterministic file content for anchor reads. The `provision`
    plugin_state carries a `files` dict keyed by path → text; `read_text`
    looks it up."""

    meta = PluginMeta(id="in_process", type="workspace", display_name="stub")

    async def provision(self, spec):  # type: ignore[no-untyped-def]
        return {"sha": spec.sha, "files": {}}

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


class _StaticContextProvider:
    def __init__(self, context: WorkspaceTicketContext) -> None:
        self._context = context

    async def get_workspace_ticket_context(self, ticket_id):  # type: ignore[no-untyped-def]
        del ticket_id
        return self._context


@pytest.fixture
def _stubs():
    clear_workspace_providers()
    clear_workflow_context_provider()
    register_workspace_provider(_StubWorkspaceProvider())
    yield
    clear_workspace_providers()
    clear_workflow_context_provider()


async def test_post_findings_persists_admitted_findings(db_session, _stubs) -> None:  # type: ignore[no-untyped-def]
    """One realistic FindingDraft flows through PostFindings → admission →
    FindingRow lands in the DB. Proves the wrapper's end-to-end plumbing."""
    org_id = uuid4()

    # 1. Ticket + PR rows so the findings FK has somewhere to land.
    ticket_id = uuid4()
    db_session.add(
        TicketRow(
            id=ticket_id,
            org_id=org_id,
            source="github_pr",
            source_external_id="42",
            title="t",
            status="pending",
            plugin_id="github",
            repo_external_id="me/repo",
            type="github_pr",
            idempotency_key=f"key-{uuid4()}",
            payload={"head_sha": "deadbeef"},
        )
    )
    await db_session.flush()
    pr_id = uuid4()
    db_session.add(
        PullRequestRow(
            id=pr_id,
            org_id=org_id,
            plugin_id="github",
            external_id="pr-external",
            repo_external_id="me/repo",
            ticket_id=ticket_id,
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
        )
    )

    # 2. Workspace row with plugin_state carrying file contents the anchor
    #    references. The stub provider's read_text looks up here.
    ws_id = uuid4()
    db_session.add(
        WorkspaceRow(
            id=ws_id,
            org_id=org_id,
            provider_id="in_process",
            spec={"sha": "deadbeef"},
            status=WorkspaceStatus.ACTIVE.value,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            plugin_state={
                "sha": "deadbeef",
                "files": {
                    "src/foo.py": "def foo(x):\n    return x.value\n",
                },
            },
        )
    )
    await db_session.commit()

    # 2. Context provider returns a real pr_id + org_id (needed for the
    #    aggregate load + persist) and head_sha matching the workspace.
    register_workflow_context_provider(
        _StaticContextProvider(
            WorkspaceTicketContext(
                org_id=org_id,
                plugin_id="github",
                repo_external_id="me/repo",
                payload={"head_sha": "deadbeef"},
                pr_id=pr_id,
            )
        )
    )

    # 3. One realistic FindingDraft — passes the 20-char scenario gate,
    #    anchored in a file the stub workspace returns.
    drafts = [
        {
            "severity": "major",
            "rule_id": "r1",
            "title": "Missing None check",
            "body": "Caller may pass None.",
            "concrete_failure_scenario": (
                "Caller can pass None; foo() dereferences without a check; raises NoneType error."
            ),
            "confidence": 90,
            "rationale": "Function signature accepts any.",
            "anchor": {"file_path": "src/foo.py", "line_start": 2, "line_end": 2},
        }
    ]

    ctx = CommandContext(
        workflow_execution_id=str(uuid4()),
        ticket_id=str(uuid4()),
        step_id="post",
        attempt=0,
    )

    # 3b. Register stub VCS plugin so the GitHub-post half of PostFindings
    # has somewhere to post. Without it, the post step raises (plugin not
    # registered) and the workflow fails. PR row's plugin_id is "github" so
    # we register under that id.
    from app.testing.stub_vcs import register_stub_vcs  # noqa: PLC0415

    with register_stub_vcs(plugin_id="github") as stub:
        outcome = await PostFindings().execute({"draft_findings": drafts, "workspace_id": str(ws_id)}, ctx)

    assert outcome.label == "success", f"unexpected failure: {outcome.failure_reason}"
    assert outcome.outputs.get("admitted_count") == 1
    assert outcome.outputs.get("dropped_count") == 0
    assert outcome.outputs.get("posted") is True
    assert len(stub.posted_reviews) == 1
    external_id, posted_review = stub.posted_reviews[0]
    assert external_id == "pr-external"
    assert len(posted_review.findings) == 1

    # 4. FindingRow landed in the DB scoped to (pr_id, org_id).
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
    assert rows[0].rule_id == "r1"
    assert rows[0].title == "Missing None check"
