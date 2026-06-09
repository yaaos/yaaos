"""`PostFindings` happy-path — `ReportedFinding`s flow end-to-end and land
as canonical `FindingRow` rows with correct severity/confidence/display_id.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.vcs import VCSPullRequest
from app.core.workflow import CommandContext
from app.core.workspace import (
    WorkspaceTicketContext,
    register_workflow_context_provider,
)
from app.domain.reviewer.commands import PostFindings
from app.domain.reviewer.models import FindingRow
from app.domain.tickets import create as create_ticket
from app.domain.tickets import upsert as upsert_pr


class _StaticContextProvider:
    def __init__(self, context: WorkspaceTicketContext) -> None:
        self._context = context

    async def get_workspace_ticket_context(self, ticket_id):  # type: ignore[no-untyped-def]
        del ticket_id
        return self._context


@pytest.mark.service
async def test_post_findings_persists_canonical_finding_rows(
    db_session, workflow_context_provider_isolation
) -> None:  # type: ignore[no-untyped-def]
    """Two `ReportedFinding`s flow through `PostFindings` → canonical `FindingRow`
    rows land in the DB with correct severity, confidence, and monotonic
    `finding_display_id` values.
    """
    org_id = uuid4()

    # 1. Ticket + PR rows so the findings FK has somewhere to land.
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
        repo_external_id="me/repo",
        session=db_session,
    )
    pr = await upsert_pr(
        VCSPullRequest(
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

    # 2. Context provider returns the real pr_id so PostFindings can resolve it.
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

    # 3. Two canonical ReportedFinding dicts — severity + confidence are valid enum strings.
    #    Encoded as stream-json stdout (the format `PostFindings` now parses via the plugin).
    findings_payload = {
        "findings": [
            {
                "file": "src/foo.py",
                "line": 10,
                "category": "security",
                "severity": "blocker",
                "confidence": "verified",
                "rationale": "Unvalidated input passed to SQL query.",
                "rule_violated": "sql-injection",
                "rule_source": "owasp",
                "suggested_fix": "Use parameterized queries.",
            },
            {
                "file": None,
                "line": None,
                "category": "correctness",
                "severity": "nit",
                "confidence": "speculative",
                "rationale": "Minor naming inconsistency.",
                "rule_violated": "naming/convention",
                "rule_source": "yaaos",
                "suggested_fix": "Rename to snake_case.",
            },
        ]
    }
    # Stream-json format: each line is a JSON object; `type=result` carries the payload.
    stdout = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init", "session_id": "s1", "model": "opus"}),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "result": json.dumps(findings_payload),
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

    # 4. Register stub VCS plugin so the GitHub-post half of PostFindings succeeds.
    from app.testing.stub_vcs import register_stub_vcs  # noqa: PLC0415

    with register_stub_vcs(plugin_id="github") as stub:
        outcome = await PostFindings().execute({"stdout": stdout}, ctx)

    assert outcome.label == "success", f"unexpected failure: {outcome.failure_reason}"
    assert outcome.outputs.get("admitted_count") == 2

    # 5. Both FindingRow rows landed in the DB with canonical schema.
    rows = (
        (
            await db_session.execute(
                select(FindingRow)
                .where(FindingRow.pr_id == pr_id, FindingRow.org_id == org_id)
                .order_by(FindingRow.finding_display_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    first, second = rows
    # finding_display_id is monotonic (1, 2)
    assert first.finding_display_id == 1
    assert second.finding_display_id == 2
    # Canonical schema fields
    assert first.severity == "blocker"
    assert first.confidence == "verified"
    assert first.category == "security"
    assert first.file == "src/foo.py"
    assert first.line == 10
    assert second.severity == "nit"
    assert second.file is None

    # 6. VCS plugin received one post_finding call per finding.
    assert len(stub.posted_findings) == 2
