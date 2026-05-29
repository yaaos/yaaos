"""Service tests for GET /api/sse/workspace_activity/{id} — streaming + cross-org isolation.

Two contracts:
1. Cross-org request yields an empty 200 stream — cross-org isolation is the
   per-org channel key, not a 404 guard.
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

from app.core.auth import AuthMiddleware, Role, register_handler
from app.core.identity import repository as identity_repo
from app.core.redis import reset_pubsub
from app.core.sse import publish_workspace_activity
from app.core.sse.web import _workspace_activity_stream
from app.domain.orgs import repository as orgs_repo


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


@pytest_asyncio.fixture
async def cross_org_seed(db_session) -> AsyncIterator[dict[str, object]]:
    """Caller in org A; the requested wfx id belongs to a different org."""
    user = await identity_repo.insert_user(db_session, display_name="OrgAOwner")

    org_a = await orgs_repo.insert_org(db_session, slug=f"wfa-a-{uuid.uuid4().hex[:8]}")
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org_a.org_id, role=Role.OWNER, handle="owner-a"
    )

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
    # `wfx_id` deliberately points at no row; the route never reads it, only
    # the (caller_org, wfx_id) channel key does.
    yield {"org_a": org_a, "foreign_wfx_id": uuid.uuid4(), "token": raw_token}


@pytest.mark.service
@pytest.mark.asyncio
async def test_non_owned_wfx_yields_empty_stream(cross_org_seed, redis_or_skip) -> None:
    """Authenticated as org A; request a wfx that no one in org A owns → empty stream.

    Cross-org isolation is the channel key: subscribing to `{org_a}:…:{wfx}` reaches
    a channel nobody publishes to. The stream is open and authorized; it just emits
    nothing. Drives `_workspace_activity_stream` directly because httpx-ASGITransport
    hangs on close for infinite streams.
    """
    gen = _workspace_activity_stream(cross_org_seed["org_a"].org_id, cross_org_seed["foreign_wfx_id"])
    collector = asyncio.create_task(gen.__anext__())
    # Yield control to let the subscription register, then publish to a
    # different org's channel for the same wfx id — should NOT reach the stream.
    await asyncio.sleep(0.1)
    await publish_workspace_activity(
        org_id=uuid.uuid4(),
        workflow_execution_id=cross_org_seed["foreign_wfx_id"],
        payload={"kind": "agent_event", "id": str(uuid.uuid4()), "body": "leak?"},
    )
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(collector, timeout=0.5)
    collector.cancel()
    await gen.aclose()


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
