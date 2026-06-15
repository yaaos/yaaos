"""Service tests for the reviewer start hook.

Verifies that the `pending → running` ticket transition fires atomically with
the workflow bootstrap commit when `pr_review_v1` starts.

Covers:
- After engine.start + drain, ticket status is `running`.
- Exactly one `ticket.status_changed` audit row (pending→running) — create_from_pr
  writes `ticket.created`, not `ticket.status_changed`, so no duplicate at creation.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.core.audit_log import list_for_entity
from app.core.tasks import drain_once, get_pending_task_names
from app.domain.reviewer.start_hook import register_reviewer_start_hooks
from app.domain.reviewer.workflows import pr_review_v1
from app.domain.tickets import create_from_pr, set_workflow_execution
from app.domain.tickets import get as get_ticket
from app.testing.workflow_harness import scoped_engine

pytestmark = pytest.mark.service


async def _drain(db_session, *, max_iters: int = 20) -> None:
    """Drain outbox rows into matching task bodies."""
    from app.core.tasks import get_broker  # noqa: PLC0415

    async def _dispatcher(kind: str, payload: dict) -> None:
        assert kind == "taskiq_enqueue"
        decorated = get_broker().find_task(payload["task_name"])
        assert decorated is not None
        await decorated.original_func(**payload["args"])

    for _ in range(max_iters):
        pending = await get_pending_task_names(db_session)
        if not pending:
            return
        delivered = await drain_once(db_session, dispatcher=_dispatcher)
        await db_session.commit()
        if delivered == 0:
            return


@pytest.mark.asyncio
async def test_workflow_start_flips_ticket_to_running(
    db_session,
    start_hooks_isolation,
    workspace_providers_isolation,
    workflow_context_provider_isolation,
) -> None:  # type: ignore[no-untyped-def]
    """Bootstrap: ticket is pending at create_from_pr, running after route_workflow
    drains and the start hook fires."""
    from app.core.audit_log import ActorKind  # noqa: PLC0415
    from app.core.auth import org_context  # noqa: PLC0415
    from app.core.workspace import (  # noqa: PLC0415
        ALL_LIFECYCLE_COMMANDS,
        register_workflow_context_provider,
    )
    from app.domain.reviewer.commands import (  # noqa: PLC0415
        ALL_LOCAL_COMMANDS,
        ALL_WORKSPACE_COMMANDS,
    )

    register_reviewer_start_hooks()

    org_id = uuid4()

    ticket_id, created = await create_from_pr(
        org_id=org_id,
        source_external_id=f"repo/r#{uuid4().hex[:6]}",
        title="Test PR",
        description=None,
        repo_external_id="repo/r",
        plugin_id="github",
        idempotency_key=f"delivery-{uuid4().hex}",
        payload={"head_sha": "abc", "is_draft": False, "is_fork": False},
        session=db_session,
    )
    assert created is True

    # Confirm pending at creation.
    await db_session.flush()
    ticket = await get_ticket(ticket_id, org_id=org_id)
    assert ticket.status == "pending"

    class _StaticCtxProvider:
        async def get_workspace_ticket_context(self, tid):  # type: ignore[no-untyped-def]
            from app.core.workspace import WorkspaceTicketContext  # noqa: PLC0415

            return WorkspaceTicketContext(
                ticket_id=tid,
                org_id=org_id,
                payload={
                    "head_sha": "abc",
                    "is_draft": False,
                    "is_fork": False,
                    "action": "opened",
                    "pr_external_id": "repo/r#1",
                    "html_url": "http://example.com",
                    "base_sha": "def",
                    "author_login": "user",
                    "author_type": "user",
                    "labels": [],
                    "head_repo_full": "repo/r",
                    "base_repo_full": "repo/r",
                    "event": "pull_request",
                },
            )

    register_workflow_context_provider(_StaticCtxProvider())

    with scoped_engine() as engine:
        for cmd in (*ALL_LIFECYCLE_COMMANDS, *ALL_WORKSPACE_COMMANDS, *ALL_LOCAL_COMMANDS):
            engine.register_command(cmd)
        engine.register_workflow(pr_review_v1)

        async with org_context(org_id, ActorKind.SYSTEM):
            wfx_id_str = await engine.start(
                workflow_name="pr_review_v1",
                ticket_id=str(ticket_id),
                ticket_payload={"head_sha": "abc", "is_draft": False, "is_fork": False},
                session=db_session,
            )
            # Stamp the ticket so the hook can find the right execution.
            await set_workflow_execution(
                ticket_id,
                workflow_execution_id=UUID(wfx_id_str),
                session=db_session,
            )
            await db_session.commit()

            # Drain exactly one route_workflow task — which triggers the
            # bootstrap branch and fires the start hook.
            await _drain(db_session, max_iters=2)

    ticket = await get_ticket(ticket_id, org_id=org_id)
    assert ticket.status == "running", f"Expected running, got {ticket.status!r}"


@pytest.mark.asyncio
async def test_two_status_changed_audit_rows_no_duplicates(
    db_session,
    start_hooks_isolation,
    workspace_providers_isolation,
    workflow_context_provider_isolation,
) -> None:  # type: ignore[no-untyped-def]
    """Exactly one ticket.status_changed audit row (pending→running from start hook).
    create_from_pr writes ticket.created only — no duplicate status_changed at creation."""
    from app.core.audit_log import ActorKind  # noqa: PLC0415
    from app.core.auth import org_context  # noqa: PLC0415
    from app.core.workspace import (  # noqa: PLC0415
        ALL_LIFECYCLE_COMMANDS,
        register_workflow_context_provider,
    )
    from app.domain.reviewer.commands import (  # noqa: PLC0415
        ALL_LOCAL_COMMANDS,
        ALL_WORKSPACE_COMMANDS,
    )

    register_reviewer_start_hooks()

    org_id = uuid4()

    ticket_id, created = await create_from_pr(
        org_id=org_id,
        source_external_id=f"repo/r#{uuid4().hex[:6]}",
        title="Test PR",
        description=None,
        repo_external_id="repo/r",
        plugin_id="github",
        idempotency_key=f"delivery-{uuid4().hex}",
        payload={"head_sha": "abc", "is_draft": False, "is_fork": False},
        session=db_session,
    )
    assert created is True

    class _StaticCtxProvider:
        async def get_workspace_ticket_context(self, tid):  # type: ignore[no-untyped-def]
            from app.core.workspace import WorkspaceTicketContext  # noqa: PLC0415

            return WorkspaceTicketContext(
                ticket_id=tid,
                org_id=org_id,
                payload={
                    "head_sha": "abc",
                    "is_draft": False,
                    "is_fork": False,
                    "action": "opened",
                    "pr_external_id": "repo/r#1",
                    "html_url": "http://example.com",
                    "base_sha": "def",
                    "author_login": "user",
                    "author_type": "user",
                    "labels": [],
                    "head_repo_full": "repo/r",
                    "base_repo_full": "repo/r",
                    "event": "pull_request",
                },
            )

    register_workflow_context_provider(_StaticCtxProvider())

    with scoped_engine() as engine:
        for cmd in (*ALL_LIFECYCLE_COMMANDS, *ALL_WORKSPACE_COMMANDS, *ALL_LOCAL_COMMANDS):
            engine.register_command(cmd)
        engine.register_workflow(pr_review_v1)

        async with org_context(org_id, ActorKind.SYSTEM):
            wfx_id_str = await engine.start(
                workflow_name="pr_review_v1",
                ticket_id=str(ticket_id),
                ticket_payload={"head_sha": "abc", "is_draft": False, "is_fork": False},
                session=db_session,
            )
            await set_workflow_execution(
                ticket_id,
                workflow_execution_id=UUID(wfx_id_str),
                session=db_session,
            )
            await db_session.commit()

            await _drain(db_session, max_iters=2)

    entries = await list_for_entity("ticket", ticket_id, org_id=org_id, kinds=["ticket.status_changed"])
    # Exactly one: pending→running (start hook). create_from_pr writes ticket.created, not
    # ticket.status_changed, so there's no duplicate event from creation.
    assert len(entries) == 1, (
        f"Expected 1 ticket.status_changed audit row; got {len(entries)}: {[e.payload for e in entries]}"
    )
    assert entries[0].payload["from_status"] == "pending"
    assert entries[0].payload["to_status"] == "running"
