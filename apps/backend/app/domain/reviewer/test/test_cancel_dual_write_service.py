"""`POST /api/reviewer/cancel` dual-writes to `cancel_pending` +
`workflow.request_cancel` for any non-terminal workflow_executions
on the ticket.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

import app.web  # noqa: F401  — registers the reviewer router
from app.core.auth import AuthMiddleware, Role
from app.core.identity import repository as identity_repo
from app.core.identity import sessions as session_lifecycle
from app.core.workflow import (
    CommandCategory,
    CommandContext,
    Outcome,
    Step,
    TerminalAction,
    Workflow,
    get_execution_summary,
)
from app.domain.orgs import repository as orgs_repo
from app.domain.tickets import create as create_ticket
from app.testing.workflow_harness import scoped_engine


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"reviewer"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


# Stable test-fixture org id. The /api/reviewer routers are org-scoped;
# production code doesn't reference this constant.
_DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000001"
_ORG_SLUG = "dual-write-test"


async def _seed_ticket(db_session) -> tuple:  # type: ignore[return]
    """Insert a ticket + a Builder session so the cancel endpoint can
    authenticate. Returns (ticket_id, session)."""
    existing = await orgs_repo.get_org_by_slug(db_session, _ORG_SLUG)
    if existing is None:
        org = await orgs_repo.insert_org(db_session, slug=_ORG_SLUG)
        existing = org
    user = await identity_repo.insert_user(db_session, display_name="Builder")
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=existing.org_id, role=Role.BUILDER, handle="b"
    )
    sess = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)

    ext_id = f"pr-{uuid4()}"
    ticket_id, _ = await create_ticket(
        type="pr_review",
        payload={},
        idempotency_key=ext_id,
        org_id=existing.org_id,
        title="cancel-test",
        source="github_pr",
        source_external_id=ext_id,
        plugin_id="github",
        repo_external_id="me/repo",
        session=db_session,
    )
    return ticket_id, existing, sess


def _auth(sess) -> dict:  # type: ignore[no-untyped-def]
    return {
        "cookies": {"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
        "headers": {"X-Org-Slug": _ORG_SLUG, "X-CSRF-Token": sess.csrf_token},
    }


@pytest.mark.asyncio
async def test_cancel_endpoint_sets_cancel_requested_on_workflow_executions(  # type: ignore[no-untyped-def]
    db_session,
):
    """A running workflow_executions row for the ticket gets
    `cancel_requested=true` after POST /api/reviewer/cancel."""
    ticket_id, _org, sess = await _seed_ticket(db_session)

    class _NoopCmd:
        kind = "CancelTestNoop"
        category = CommandCategory.LOCAL
        restart_safe = True

        async def execute(self, inputs, ctx: CommandContext) -> Outcome:
            del inputs, ctx
            return Outcome.success()

    _stub_wf = Workflow(
        name="cancel_test_v1",
        version=1,
        steps=(
            Step(
                id="s1",
                command_kind="CancelTestNoop",
                transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
            ),
        ),
        entry_step_id="s1",
    )

    with scoped_engine() as engine:
        engine.register_command(_NoopCmd())
        engine.register_workflow(_stub_wf)
        # Create two workflow executions for the ticket.
        wfx_id_running = await engine.start(
            workflow_name="cancel_test_v1",
            ticket_id=str(ticket_id),
            session=db_session,
        )
        wfx_id_pending2 = await engine.start(
            workflow_name="cancel_test_v1",
            ticket_id=str(ticket_id),
            session=db_session,
        )
        await db_session.commit()

        async with _client() as c:
            resp = await c.post(f"/api/reviewer/cancel?ticket_id={ticket_id}", **_auth(sess))
        assert resp.status_code == 200, resp.text
        assert resp.json()["cancelled_count"] >= 1

        # The cancel endpoint marks non-terminal executions.
        wfx1 = await get_execution_summary(wfx_id_running, session=db_session)
        wfx2 = await get_execution_summary(wfx_id_pending2, session=db_session)
        assert wfx1 is not None
        assert wfx2 is not None
        assert wfx1.cancel_requested is True
        assert wfx2.cancel_requested is True


@pytest.mark.asyncio
async def test_cancel_endpoint_no_workflows_returns_zero(db_session) -> None:  # type: ignore[no-untyped-def]
    """No workflows + no review_jobs rows → cancelled_count == 0."""
    ticket_id, _, sess = await _seed_ticket(db_session)
    await db_session.commit()

    async with _client() as c:
        resp = await c.post(f"/api/reviewer/cancel?ticket_id={ticket_id}", **_auth(sess))
    assert resp.status_code == 200
    assert resp.json()["cancelled_count"] == 0


@pytest.mark.asyncio
async def test_cancel_endpoint_404_on_missing_ticket(db_session) -> None:  # type: ignore[no-untyped-def]
    # Seed an org + session so we get past auth and into the handler.
    _, _, sess = await _seed_ticket(db_session)
    await db_session.commit()
    async with _client() as c:
        resp = await c.post(f"/api/reviewer/cancel?ticket_id={uuid4()}", **_auth(sess))
    assert resp.status_code == 404
