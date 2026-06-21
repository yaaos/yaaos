"""Service test: failure-shaped catches in domain/reviewer record exception events on spans.

Covers:
- SecretsScan: when `post_comment` raises after detecting a secret, the
  surrounding span receives an exception event + ERROR status (the outcome
  is still success/skip — the comment failure is logged but not fatal).
- PostFindings: a VCS post_finding failure propagates from PostFindings
  (the broad outer catch was removed; only ValueError is caught). The engine's
  _safe_execute records exactly ONE exception event on the PostFindings span.
  _post_findings_via_vcs must NOT call record_exception before re-raising —
  that would produce two events when the engine records it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from app.testing.observability import span_capture

pytestmark = pytest.mark.service


# ── SecretsScan.post_warning_failed path ─────────────────────────────────────


@pytest.mark.asyncio
async def test_secrets_scan_post_comment_failure_sets_span_error(db_session) -> None:  # type: ignore[no-untyped-def]
    """When post_comment raises after detecting a secret, SecretsScan records
    exception + ERROR on the active span but still returns success(label='skip')."""
    from app.core.vcs import Diff, set_vcs_for_tests  # noqa: PLC0415
    from app.core.workflow import CommandContext  # noqa: PLC0415
    from app.domain.reviewer.commands import SecretsScan, SecretsScanInputs  # noqa: PLC0415

    class _RaisingOnComment:
        """VCS plugin that returns a diff with a secret but raises on post_comment."""

        plugin_id = "github"

        async def fetch_diff(self, org_id: UUID, external_id: str) -> Diff:
            del org_id, external_id
            return Diff(raw="+AWS_KEY = 'AKIAQWERTYUIOPASDFGH'\n", files=[])

        async def post_comment(self, org_id: UUID, external_id: str, *, body: str) -> str:
            raise RuntimeError("simulated post_comment failure")

        # Remaining protocol stubs — not exercised by this test.
        def install_url(self, org_id: UUID) -> str | None:
            return None

        def validate_settings(self, settings: dict) -> dict:  # type: ignore[type-arg]
            return dict(settings)

        async def fetch_pr(self, org_id: UUID, external_id: str):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        async def fetch_diff_stub(self, org_id: UUID, external_id: str):  # type: ignore[no-untyped-def]
            raise NotImplementedError

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

        async def post_comment_reply(self, org_id: UUID, external_id: str, parent: str, body: str) -> str:
            return "stub-reply"

        async def mark_comments_outdated(
            self,
            org_id: UUID,
            external_id: str,
            ids: list,  # type: ignore[type-arg]
        ) -> None:
            pass

        async def get_installation_token(self, org_id: UUID) -> str:
            return "stub-token"

        async def list_installation_repos(self, org_id: UUID) -> list:  # type: ignore[type-arg]
            return []

        async def post_finding(self, *args: object, **kwargs: object) -> str:
            return "stub-comment"

    inputs = SecretsScanInputs(org_id=uuid4(), plugin_id="github", pr_external_id="pr-1")
    ctx = CommandContext(
        ticket_id=str(uuid4()),
        workflow_execution_id=str(uuid4()),
        step_id="secrets_scan",
        attempt=0,
    )

    with span_capture() as exporter:
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span("workflow.command.SecretsScan"):
            with set_vcs_for_tests(plugin=_RaisingOnComment()):  # type: ignore[arg-type]
                outcome = await SecretsScan().execute(inputs, ctx, session=db_session)

    # Outcome is still success/skip — the post_comment failure is non-fatal.
    assert outcome.label == "skip", f"expected skip, got {outcome.label!r}"

    spans = exporter.get_finished_spans()
    target = next((s for s in spans if "SecretsScan" in s.name), None)
    assert target is not None, f"no SecretsScan span; got: {[s.name for s in spans]}"

    exception_events = [e for e in target.events if e.name == "exception"]
    assert exception_events, (
        f"expected exception event on SecretsScan span, got: {[e.name for e in target.events]}"
    )
    assert target.status.status_code == StatusCode.ERROR, (
        f"expected ERROR status on SecretsScan span, got {target.status.status_code}"
    )


# ── PostFindings VCS-post failure — exactly ONE exception event on the span ───


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
    db_session,
) -> None:
    """A VCS post_finding failure must produce exactly ONE exception event on the
    workflow.command.PostFindings span.

    Before the fix, _post_findings_via_vcs called record_exception before
    re-raising, and PostFindings.execute's outer catch called it again —
    producing two duplicate exception events on the same span.
    """
    from app.core.vcs import VCSPullRequest, set_vcs_for_tests  # noqa: PLC0415
    from app.core.workflow import CommandContext  # noqa: PLC0415
    from app.domain.reviewer.commands import PostFindings, PostFindingsInputs  # noqa: PLC0415
    from app.domain.tickets import create_from_pr as create_ticket  # noqa: PLC0415
    from app.domain.tickets import upsert as upsert_pr  # noqa: PLC0415

    org_id = uuid4()
    ext_id = f"42-{uuid4().hex[:6]}"

    ticket_id, _ = await create_ticket(
        org_id=org_id,
        source_external_id=ext_id,
        title="t",
        description=None,
        repo_external_id="owner/repo",
        plugin_id="github",
        idempotency_key=ext_id,
        payload={"head_sha": "deadbeef"},
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

    from app.domain.reviewer.types import ReportedFindingShape  # noqa: PLC0415

    finding = ReportedFindingShape(
        file="src/foo.py",
        line=1,
        category="security",
        severity="blocker",
        confidence="verified",
        rationale="r",
        rule_violated="r",
        rule_source="yaaos",
        suggested_fix="Use parameterized queries.",
    )

    ctx = CommandContext(
        workflow_execution_id=str(uuid4()),
        ticket_id=str(ticket_id),
        step_id="post",
        attempt=0,
    )

    inputs = PostFindingsInputs(
        findings=[finding],
        org_id=org_id,
        pr_id=pr.id,
        pr_external_id=f"pr-{ext_id}",
        vcs_plugin_id="github",
    )

    # Swap in the raising VCS plugin.
    with set_vcs_for_tests(plugin=_RaisingVCSPlugin()):  # type: ignore[arg-type]
        with span_capture() as exporter:
            tracer = trace.get_tracer(__name__)
            # OTel records the propagating exception automatically on the span
            # (record_exception=True is the default) — this simulates what
            # _safe_execute sees when PostFindings raises.
            with pytest.raises(RuntimeError, match="simulated VCS post failure"):
                with tracer.start_as_current_span("workflow.command.PostFindings"):
                    await PostFindings().execute(inputs, ctx, session=db_session)

    # The exception propagated: PostFindings no longer catches non-ValueError
    # exceptions. The engine's _safe_execute records it on the same span.
    spans = exporter.get_finished_spans()
    target = next((s for s in spans if "PostFindings" in s.name), None)
    assert target is not None, f"no PostFindings span; got: {[s.name for s in spans]}"

    # OTel auto-records exactly ONE exception event (from the propagating exception
    # through start_as_current_span). _post_findings_via_vcs must not call
    # record_exception before re-raising — that would produce two events here.
    exception_events = [e for e in target.events if e.name == "exception"]
    assert len(exception_events) == 1, (
        f"expected exactly 1 exception event (OTel auto-record); "
        f"_post_findings_via_vcs must not double-record. "
        f"Got {len(exception_events)}: {[e.attributes for e in exception_events]}"
    )
    assert target.status.status_code == StatusCode.ERROR, (
        f"expected ERROR status from propagated exception, got {target.status.status_code}"
    )
