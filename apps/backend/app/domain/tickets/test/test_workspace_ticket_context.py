"""`get_workspace_ticket_context` — read path for the workflow-context provider.

Returns the ticket's org_id / plugin_id / repo_external_id / payload / pr_id
for use by core/workspace WorkflowCommand bodies. Returns None when the
ticket doesn't exist.
"""

from __future__ import annotations

from uuid import uuid4

from app.domain.tickets import create_from_pr as create_ticket
from app.domain.tickets.service import get_workspace_ticket_context


async def test_returns_none_for_missing_ticket() -> None:
    ctx = await get_workspace_ticket_context(uuid4())
    assert ctx is None


async def test_returns_ticket_fields_for_real_ticket(db_session) -> None:  # type: ignore[no-untyped-def]
    org_id = uuid4()
    ticket_id, _ = await create_ticket(
        org_id=org_id,
        source_external_id="42",
        title="t",
        description=None,
        repo_external_id="me/repo",
        plugin_id="github",
        idempotency_key=f"ctx-{uuid4()}",
        payload={"head_sha": "abc123", "is_draft": False},
        session=db_session,
    )
    await db_session.commit()

    ctx = await get_workspace_ticket_context(ticket_id)
    assert ctx is not None
    assert ctx.org_id == org_id
    assert ctx.plugin_id == "github"
    assert ctx.repo_external_id == "me/repo"
    assert ctx.payload["head_sha"] == "abc123"
    # No PR row linked yet — pr_id should be None.
    assert ctx.pr_id is None


async def test_pr_id_passthrough_when_linked(db_session) -> None:  # type: ignore[no-untyped-def]
    """A ticket explicitly linked to a PR row exposes that PR id so future
    Local commands (ResolveFinding, ArchiveStaleFindings, PostReply,
    PostFindings) can load the reviewer aggregate."""
    from app.domain.tickets.models import TicketRow  # noqa: PLC0415

    org_id = uuid4()
    pr_id = uuid4()
    ticket = TicketRow(
        id=uuid4(),
        org_id=org_id,
        source="github_pr",
        source_external_id="99",
        title="linked",
        description=None,
        status="pending",
        plugin_id="github",
        repo_external_id="me/repo",
        pr_id=pr_id,
        type="github_pr",
        idempotency_key=f"linked-{uuid4()}",
        payload={},
        current_workflow_execution_id=None,
    )
    db_session.add(ticket)
    await db_session.commit()

    ctx = await get_workspace_ticket_context(ticket.id)
    assert ctx is not None
    assert ctx.pr_id == pr_id
