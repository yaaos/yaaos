"""Service test: failure-shaped catches in domain/reviewer record exception events on spans.

Samples the SecretsScan.context_fetch_failed path as a representative
failure catch: forces the workflow context provider to raise, then asserts
the surrounding span carries an `exception` event with ERROR status.

Also covers the PostFindings VCS-post failure path to assert that a single
VCS error produces exactly ONE exception event on the PostFindings span
(not two — the inner _post_findings_via_vcs catch must not call
record_exception before re-raising into the outer PostFindings.execute catch).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from app.testing.observability import span_capture

pytestmark = pytest.mark.service


class _RaisingProvider:
    """WorkflowContextProvider stub that always raises."""

    async def get_workspace_ticket_context(self, ticket_id: UUID):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated context fetch failure")


@pytest.mark.asyncio
async def test_reviewer_failure_catch_records_on_span() -> None:
    """SecretsScan.context_fetch_failed records exception event + ERROR on the active span."""
    from app.core.workspace import register_workflow_context_provider  # noqa: PLC0415
    from app.domain.reviewer.commands import SecretsScan  # noqa: PLC0415

    # Install the raising stub (isolation fixture resets to None after the test).
    register_workflow_context_provider(_RaisingProvider())

    cmd = SecretsScan()
    from app.core.workflow import CommandContext  # noqa: PLC0415

    ctx = CommandContext(
        ticket_id="00000000-0000-0000-0000-000000000001",
        workflow_execution_id="00000000-0000-0000-0000-000000000002",
        step_id="secrets_scan",
        attempt=0,
    )

    with span_capture() as exporter:
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span("workflow.command.SecretsScan"):
            outcome = await cmd.execute({}, ctx)

    assert outcome.kind.name == "FAILURE", f"expected FAILURE outcome, got {outcome.kind}"

    spans = exporter.get_finished_spans()
    target = next((s for s in spans if "SecretsScan" in s.name), None)
    assert target is not None, f"no SecretsScan span; got: {[s.name for s in spans]}"

    exception_events = [e for e in target.events if e.name == "exception"]
    assert exception_events, f"expected exception event on span, got: {[e.name for e in target.events]}"
    assert target.status.status_code == StatusCode.ERROR, (
        f"expected ERROR status, got {target.status.status_code}"
    )


# ---------------------------------------------------------------------------
# PostFindings VCS-post failure — exactly ONE exception event on the span
# ---------------------------------------------------------------------------


class _RaisingVCSPlugin:
    """Minimal VCSPlugin stub whose post_finding always raises."""

    plugin_id = "github"

    async def post_finding(self, *args: object, **kwargs: object) -> str:
        raise RuntimeError("simulated VCS post failure")

    # Remaining protocol methods — stubs that satisfy the Protocol minimally.
    def install_url(self, org_id: UUID) -> str | None:
        return None

    def validate_settings(self, settings: dict) -> dict:  # type: ignore[type-arg]
        return dict(settings)

    async def fetch_pr(self, org_id: UUID, external_id: str):  # type: ignore[no-untyped-def]
        from app.core.vcs import VCSPullRequest  # noqa: PLC0415

        return VCSPullRequest(
            plugin_id="github",
            external_id=external_id,
            repo_external_id="owner/repo",
            number=1,
            title="stub",
            body=None,
            author_login="alice",
            author_type="user",
            base_branch="main",
            head_branch="feature",
            base_sha="base",
            head_sha="head",
            is_draft=False,
            is_fork=False,
            state="open",
            html_url="http://test",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    async def fetch_diff(self, org_id: UUID, external_id: str):  # type: ignore[no-untyped-def]
        from app.core.vcs import Diff, FileSummary  # noqa: PLC0415

        return Diff(
            raw="diff --git a/f b/f\n",
            files=[FileSummary(path="f", status="modified", additions=1, deletions=0)],
        )

    async def list_yaaos_comments(self, org_id: UUID, external_id: str) -> list:  # type: ignore[type-arg]
        return []

    async def is_repo_accessible(self, org_id: UUID, repo_external_id: str) -> bool:
        return True

    async def detect_force_push(
        self, org_id: UUID, repo_external_id: str, before_sha: str, after_sha: str
    ) -> bool:
        return False

    async def list_commit_messages(
        self, org_id: UUID, repo_external_id: str, prev_sha: str, head_sha: str
    ) -> list:  # type: ignore[type-arg]
        return []

    async def post_comment(self, org_id: UUID, external_id: str, *, body: str) -> str:
        return "stub-comment"

    async def post_comment_reply(self, org_id: UUID, external_id: str, parent: str, body: str) -> str:
        return "stub-reply"

    async def mark_comments_outdated(self, org_id: UUID, external_id: str, ids: list) -> None:  # type: ignore[type-arg]
        pass

    async def get_installation_token(self, org_id: UUID) -> str:
        return "stub-token"

    async def list_installation_repos(self, org_id: UUID) -> list:  # type: ignore[type-arg]
        return []


@pytest.mark.asyncio
async def test_post_findings_vcs_failure_records_exactly_one_exception_event(
    db_session, workflow_context_provider_isolation
) -> None:
    """A VCS post_finding failure must produce exactly ONE exception event on the
    workflow.command.PostFindings span.

    Before the fix, _post_findings_via_vcs called record_exception before
    re-raising, and PostFindings.execute's outer catch called it again —
    producing two duplicate exception events on the same span.
    """
    from app.core.vcs import VCSPullRequest, bind_vcs_registry, current_vcs_registry  # noqa: PLC0415
    from app.core.workflow import CommandContext  # noqa: PLC0415
    from app.core.workspace import WorkspaceTicketContext, register_workflow_context_provider  # noqa: PLC0415
    from app.domain.reviewer.commands import PostFindings  # noqa: PLC0415
    from app.domain.tickets import create as create_ticket  # noqa: PLC0415
    from app.domain.tickets import upsert as upsert_pr  # noqa: PLC0415

    org_id = uuid4()
    ext_id = f"42-{uuid4().hex[:6]}"

    ticket_id, _ = await create_ticket(
        type="pr_review",
        payload={"head_sha": "deadbeef"},
        idempotency_key=ext_id,
        org_id=org_id,
        title="t",
        source="github_pr",
        source_external_id=ext_id,
        plugin_id="github",
        repo_external_id="owner/repo",
        session=db_session,
    )
    pr = await upsert_pr(
        VCSPullRequest(
            plugin_id="github",
            repo_external_id="owner/repo",
            external_id=f"pr-{ext_id}",
            number=42,
            title="t",
            body=None,
            author_login="alice",
            author_type="user",
            base_branch="main",
            head_branch="feature",
            base_sha="base",
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
    await db_session.commit()

    register_workflow_context_provider(
        type(
            "_Ctx",
            (),
            {
                "get_workspace_ticket_context": lambda self, tid: _make_coro(
                    WorkspaceTicketContext(
                        org_id=org_id,
                        plugin_id="github",
                        repo_external_id="owner/repo",
                        payload={"head_sha": "deadbeef"},
                        pr_id=pr.id,
                    )
                )
            },
        )()
    )

    stdout = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init", "session_id": "s1", "model": "opus"}),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "result": json.dumps(
                        {
                            "findings": [
                                {
                                    "file": "src/foo.py",
                                    "line": 1,
                                    "category": "security",
                                    "severity": "blocker",
                                    "confidence": "verified",
                                    "rationale": "r",
                                    "rule_violated": "r",
                                    "rule_source": "yaaos",
                                    "suggested_fix": "Use parameterized queries.",
                                }
                            ]
                        }
                    ),
                    "is_error": False,
                }
            ),
        ]
    )

    ctx = CommandContext(
        workflow_execution_id=str(uuid4()),
        ticket_id=str(ticket_id),
        step_id="post",
        attempt=0,
    )

    # Swap in the raising VCS plugin.
    raising_plugin = _RaisingVCSPlugin()
    prior_registry = current_vcs_registry()
    fresh_registry = prior_registry.copy()
    fresh_registry.replace(raising_plugin)  # type: ignore[arg-type]
    bind_vcs_registry(fresh_registry)

    try:
        with span_capture() as exporter:
            tracer = trace.get_tracer(__name__)
            with tracer.start_as_current_span("workflow.command.PostFindings"):
                outcome = await PostFindings().execute({"stdout": stdout}, ctx)
    finally:
        bind_vcs_registry(prior_registry)

    assert outcome.label == "failure", f"expected failure outcome, got {outcome.label!r}"

    spans = exporter.get_finished_spans()
    target = next((s for s in spans if "PostFindings" in s.name), None)
    assert target is not None, f"no PostFindings span; got: {[s.name for s in spans]}"

    exception_events = [e for e in target.events if e.name == "exception"]
    assert len(exception_events) == 1, (
        f"expected exactly 1 exception event on PostFindings span, got {len(exception_events)}: "
        f"{[e.attributes for e in exception_events]}"
    )
    assert target.status.status_code == StatusCode.ERROR, (
        f"expected ERROR status, got {target.status.status_code}"
    )


async def _make_coro(value: object) -> object:
    return value
