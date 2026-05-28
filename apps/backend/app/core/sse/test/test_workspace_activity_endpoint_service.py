"""Service tests for GET /api/sse/workspace_activity/{id} — ownership check + streaming.

Tests the two contracts:
1. Cross-org wfx → 404 (via the registered ownership check).
2. Happy path streams events scoped to (org, workflow_execution_id).

The streaming test drives `_workspace_activity_stream` directly (mirrors the
general-endpoint test) because httpx-ASGITransport hangs on close for
indefinite streams.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.auth import AuthMiddleware, register_handler
from app.core.identity import repository as identity_repo
from app.core.sse import (
    publish_workspace_activity,
    register_workspace_activity_ownership_check,
    reset_pubsub,
    reset_workspace_activity_ownership_check,
)
from app.core.sse.web import _workspace_activity_stream
from app.core.workflow import WorkflowExecutionRow
from app.domain.orgs import Role, assert_workflow_in_org
from app.domain.orgs import repository as orgs_repo
from app.domain.tickets import TicketRow


def _make_app() -> FastAPI:
    """Test app with AuthMiddleware + AuthFailure handler + SSE module mounted."""
    from app.core.sse import web as _sse_web  # noqa: F401, PLC0415
    from app.core.webserver import mount_specs  # noqa: PLC0415

    app_ = FastAPI()
    app_.add_middleware(AuthMiddleware)
    register_handler(app_)
    mount_specs(app_, only={"sse"})
    return app_


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_make_app()), base_url="http://test")


@pytest.fixture(autouse=True)
def _isolate_pubsub() -> None:
    reset_pubsub()
    yield
    reset_pubsub()


@pytest.fixture(autouse=True)
def _isolate_ownership_check() -> None:
    """Register the real ownership check; reset on teardown.

    `core/sse/web` raises if the check is unregistered, so a test session
    that doesn't import `app.web` (this one) must wire it explicitly.
    """
    reset_workspace_activity_ownership_check()
    register_workspace_activity_ownership_check(assert_workflow_in_org)
    yield
    reset_workspace_activity_ownership_check()


def _make_ticket(org_id) -> TicketRow:
    return TicketRow(
        id=uuid.uuid4(),
        org_id=org_id,
        source="github_pr",
        source_external_id=f"pr-{uuid.uuid4()}",
        title="wfa-test",
        plugin_id="github",
        repo_external_id="me/repo",
    )


def _make_wfx(ticket_id) -> WorkflowExecutionRow:
    return WorkflowExecutionRow(
        ticket_id=ticket_id,
        workflow_name="pr_review_v1",
        workflow_version=1,
        state="running",
        current_step_id=None,
        pending_agent_command_id=None,
        step_state={},
        cancel_requested=False,
        otel_trace_context=None,
    )


@pytest_asyncio.fixture
async def cross_org_seed(db_session) -> AsyncIterator[dict[str, object]]:
    """Caller in org A; workflow execution belongs to org B."""
    user = await identity_repo.insert_user(db_session, display_name="OrgAOwner")

    org_a = await orgs_repo.insert_org(db_session, slug=f"wfa-a-{uuid.uuid4().hex[:8]}")
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org_a.id, role=Role.OWNER, handle="owner-a"
    )

    org_b = await orgs_repo.insert_org(db_session, slug=f"wfa-b-{uuid.uuid4().hex[:8]}")

    ticket_b = _make_ticket(org_b.id)
    db_session.add(ticket_b)
    await db_session.flush()

    wfx_b = _make_wfx(ticket_b.id)
    db_session.add(wfx_b)
    await db_session.flush()

    raw_token = f"wfa-{uuid.uuid4().hex[:8]}"
    await identity_repo.insert_session(
        db_session,
        token_hash=identity_repo.hash_token(raw_token),
        user_id=user.id,
        workspace_id=None,
        csrf_token="csrf-wfa",
        ip=None,
        user_agent=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await db_session.commit()
    yield {"org_a": org_a, "org_b": org_b, "wfx_b_id": wfx_b.id, "token": raw_token}


@pytest.mark.service
@pytest.mark.asyncio
async def test_workspace_activity_endpoint_returns_404_for_cross_org_wfx(cross_org_seed) -> None:
    """Authenticated as org A; request a wfx belonging to org B → 404."""
    async with _client() as c:
        resp = await c.get(
            f"/api/sse/workspace_activity/{cross_org_seed['wfx_b_id']}",
            cookies={"yaaos_session": cross_org_seed["token"]},
            headers={"X-Org-Slug": cross_org_seed["org_a"].slug},
        )
    assert resp.status_code == 404


@pytest.mark.service
@pytest.mark.asyncio
async def test_workspace_activity_endpoint_streams_org_scoped(redis_or_skip) -> None:
    """Happy path: publish to (org, wfx); the stream emits the event.

    Drives `_workspace_activity_stream` directly — httpx-ASGITransport hangs
    on close for infinite streams. The HTTP wiring above this is a thin
    wrapper with no logic of its own.
    """
    org_id = uuid.uuid4()
    wfx_id = uuid.uuid4()

    gen = _workspace_activity_stream(org_id, wfx_id)
    collector = asyncio.create_task(gen.__anext__())
    # Yield control so the generator registers its Redis subscription before
    # we publish — Redis pub/sub is fire-and-forget; earlier publishes drop.
    await asyncio.sleep(0.1)

    payload = {"kind": "agent_event", "id": str(uuid.uuid4()), "body": "hello"}
    await publish_workspace_activity(
        org_id=org_id,
        workflow_execution_id=wfx_id,
        payload=payload,
    )

    frame: str = await asyncio.wait_for(collector, timeout=3.0)
    await gen.aclose()

    assert frame.startswith("data: "), f"SSE frame must start with 'data: '; got {frame!r}"
    assert payload["id"] in frame, f"published payload not in frame: {frame!r}"
