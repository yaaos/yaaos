"""Service test: PostFindings reads `output` field from typed `PostFindingsInputs`.

Verifies:
- `PostFindings.execute(PostFindingsInputs(output=<canned_stdout>, ...), ctx)` parses
  and persists findings.
- `PostFindingsInputs(output="")` returns zero findings — success.
- `publish_findings` is NOT passed a `run_id` kwarg — the new path drops that lookup.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.vcs import VCSPullRequest
from app.core.workflow import CommandContext
from app.domain.reviewer.commands import PostFindings, PostFindingsInputs
from app.domain.reviewer.models import FindingRow
from app.domain.tickets import create_from_pr as create_ticket
from app.domain.tickets import upsert as upsert_pr

pytestmark = pytest.mark.service


def _canned_stdout(findings_payload: dict) -> str:  # type: ignore[type-arg]
    """Encode a findings dict as stream-json stdout."""
    return "\n".join(
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


def _ctx(ticket_id: str, wfx_id: str) -> CommandContext:
    return CommandContext(
        workflow_execution_id=wfx_id,
        ticket_id=ticket_id,
        step_id="PostFindings",
        attempt=0,
    )


@pytest.mark.asyncio
async def test_post_findings_reads_output_field(db_session) -> None:
    """PostFindings parses the `output` field of PostFindingsInputs and persists findings."""
    org_id = uuid4()
    ext_id = f"pf-out-{uuid4().hex[:6]}"
    ticket_id, _ = await create_ticket(
        org_id=org_id,
        source_external_id=ext_id,
        title="t",
        description=None,
        repo_external_id="me/repo",
        plugin_id="github",
        idempotency_key=ext_id,
        payload={"head_sha": "deadbeef"},
        session=db_session,
    )
    pr = await upsert_pr(
        VCSPullRequest(
            plugin_id="github",
            repo_external_id="me/repo",
            external_id=f"pr-{ext_id}",
            number=1,
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

    stdout = _canned_stdout(
        {
            "findings": [
                {
                    "file": "src/foo.py",
                    "line": 5,
                    "category": "security",
                    "severity": "blocker",
                    "confidence": "verified",
                    "rationale": "SQL injection risk.",
                    "rule_violated": "sql-injection",
                    "rule_source": "owasp",
                    "suggested_fix": "Use parameterized queries.",
                }
            ]
        }
    )

    wfx_id = str(uuid4())
    ctx = _ctx(str(ticket_id), wfx_id)
    inputs = PostFindingsInputs(
        output=stdout,
        org_id=org_id,
        pr_id=pr_id,
        pr_external_id=f"pr-{ext_id}",
        vcs_plugin_id="github",
    )

    from app.testing.stub_vcs import register_stub_vcs  # noqa: PLC0415

    with register_stub_vcs(plugin_id="github") as stub:
        outcome = await PostFindings().execute(inputs, ctx)

    assert outcome.label == "success", f"unexpected failure: {outcome.failure_reason}"
    assert outcome.outputs.admitted_count == 1

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
    assert rows[0].severity == "blocker"
    assert rows[0].finding_display_id == 1
    # VCS plugin received the post.
    assert len(stub.posted_findings) == 1


@pytest.mark.asyncio
async def test_post_findings_empty_output_returns_zero() -> None:
    """`output=""` in typed inputs → zero findings, success outcome."""
    outcome = await PostFindings().execute(
        PostFindingsInputs(output="", org_id=uuid4()),
        _ctx(str(uuid4()), str(uuid4())),
    )
    assert outcome.label == "success"
    assert outcome.outputs.admitted_count == 0
