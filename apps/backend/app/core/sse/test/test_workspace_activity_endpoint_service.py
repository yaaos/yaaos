"""Service tests for GET /api/sse/workspace_activity/{id} — streaming + cross-org isolation.

Two contracts:
1. Cross-org request yields an empty 200 stream — cross-org isolation is the
   per-org channel key, not a 404 guard.
2. Happy path streams events scoped to (org, run_id).

The streaming test drives `_workspace_activity_stream` directly (mirrors the
general-endpoint test) because httpx-ASGITransport hangs on close for
indefinite streams.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.auth import AuthMiddleware, Role, register_handler
from app.core.cache import CacheError, set_cache_for_tests, subscriber_count
from app.core.identity import create_user, mint_session
from app.core.sse import publish_workspace_activity
from app.core.sse.web import _workspace_activity_stream
from app.domain.orgs import insert_membership, insert_org


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


@pytest_asyncio.fixture
async def cross_org_seed(db_session) -> AsyncIterator[dict[str, object]]:
    """Caller in org A; the requested run_id id belongs to a different org."""
    user = await create_user(db_session, display_name="OrgAOwner")

    org_a = await insert_org(db_session, slug=f"wfa-a-{uuid.uuid4().hex[:8]}")
    await insert_membership(
        db_session, user_id=user.id, org_id=org_a.org_id, role=Role.OWNER, handle="owner-a"
    )

    created = await mint_session(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()
    # `run_id` deliberately points at no row; the route never reads it, only
    # the (caller_org, run_id) channel key does.
    yield {"org_a": org_a, "foreign_run_id": uuid.uuid4(), "token": created.raw_token}


@pytest.mark.service
@pytest.mark.asyncio
async def test_non_owned_run_yields_empty_stream(cross_org_seed, redis_or_skip) -> None:
    """Authenticated as org A; request a run_id that no one in org A owns → empty stream.

    Cross-org isolation is the channel key: subscribing to `{org_a}:…:{run_id}` reaches
    a channel nobody publishes to. The stream is open and authorized; it just emits
    nothing. Drives `_workspace_activity_stream` directly because httpx-ASGITransport
    hangs on close for infinite streams.
    """
    gen = _workspace_activity_stream(cross_org_seed["org_a"].org_id, cross_org_seed["foreign_run_id"])
    # Drain the connect prelude.  The subscription is established BEFORE the
    # prelude is sent (see _workspace_activity_stream), so no sleep is needed
    # before publishing — but we keep asyncio.sleep(0.1) below for clarity.
    prelude = await asyncio.wait_for(gen.__anext__(), timeout=3.0)
    assert prelude.startswith(":"), f"expected comment prelude first; got {prelude!r}"
    collector = asyncio.create_task(gen.__anext__())
    # Publish to a different org's channel for the same run_id — should NOT reach the stream.
    await asyncio.sleep(0.1)
    await publish_workspace_activity(
        org_id=uuid.uuid4(),
        run_id=cross_org_seed["foreign_run_id"],
        payload={"kind": "agent_event", "id": str(uuid.uuid4()), "body": "leak?"},
    )
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(collector, timeout=0.5)
    collector.cancel()
    await gen.aclose()


@pytest.mark.service
@pytest.mark.asyncio
async def test_workspace_activity_endpoint_streams_org_scoped(redis_or_skip) -> None:
    """Happy path: publish to (org, run_id); the stream emits the event.

    Drives `_workspace_activity_stream` directly — httpx-ASGITransport hangs
    on close for infinite streams. The HTTP wiring above this is a thin
    wrapper with no logic of its own.
    """
    org_id = uuid.uuid4()
    run_id = uuid.uuid4()

    gen = _workspace_activity_stream(org_id, run_id)
    # Drain the connect prelude.  The subscription is established BEFORE the
    # prelude is sent (see _workspace_activity_stream), so by the time we
    # get the prelude the subscription is already ready.
    prelude = await asyncio.wait_for(gen.__anext__(), timeout=3.0)
    assert prelude.startswith(":"), f"expected comment prelude first; got {prelude!r}"
    collector = asyncio.create_task(gen.__anext__())
    # No sleep needed — subscription is already established.  Keep a short
    # yield so the asyncio event loop can schedule the collector task.
    await asyncio.sleep(0)

    payload = {"kind": "agent_event", "id": str(uuid.uuid4()), "body": "hello"}
    await publish_workspace_activity(
        org_id=org_id,
        run_id=run_id,
        payload=payload,
    )

    frame: str = await asyncio.wait_for(collector, timeout=3.0)
    await gen.aclose()

    assert frame.startswith("data: "), f"SSE frame must start with 'data: '; got {frame!r}"
    assert payload["id"] in frame, f"published payload not in frame: {frame!r}"


@pytest.mark.service
@pytest.mark.asyncio
async def test_activity_subscription_failure_before_readiness_propagates() -> None:
    """A failed subscription cannot leave the connect prelude waiting forever."""
    closed = asyncio.Event()

    class FailingEvents:
        def __aiter__(self):
            return self

        async def __anext__(self) -> dict:
            raise CacheError("subscription unavailable")

        async def aclose(self) -> None:
            closed.set()

    def failing_subscription(org_id, run_id, on_subscribed=None):
        return FailingEvents()

    gen = _workspace_activity_stream(uuid.uuid4(), uuid.uuid4(), subscribe_activity=failing_subscription)
    with pytest.raises(CacheError, match="subscription unavailable"):
        await asyncio.wait_for(anext(gen), timeout=1.0)
    assert closed.is_set()


@pytest.mark.service
@pytest.mark.asyncio
async def test_activity_close_at_prelude_releases_subscription() -> None:
    """Closing before lifecycle attachment still cleans up the ready subscription."""
    with set_cache_for_tests(scenario="memory"):
        org_id, run_id = uuid.uuid4(), uuid.uuid4()
        channel = f"{org_id}:workspace_activity:{run_id}"
        gen = _workspace_activity_stream(org_id, run_id)
        prelude = await asyncio.wait_for(anext(gen), timeout=1.0)
        assert prelude.startswith(":")
        assert subscriber_count(channel) == 1
        await gen.aclose()
        assert subscriber_count(channel) == 0


@pytest.mark.service
@pytest.mark.asyncio
async def test_activity_cancel_before_readiness_closes_iterator() -> None:
    """Cancellation while waiting for readiness cancels the pending receive."""
    entered = asyncio.Event()
    closed = asyncio.Event()

    class WaitingEvents:
        def __aiter__(self):
            return self

        async def __anext__(self) -> dict:
            entered.set()
            await asyncio.Event().wait()
            return {}

        async def aclose(self) -> None:
            closed.set()

    def waiting_subscription(org_id, run_id, on_subscribed=None):
        return WaitingEvents()

    gen = _workspace_activity_stream(uuid.uuid4(), uuid.uuid4(), subscribe_activity=waiting_subscription)
    consumer = asyncio.create_task(anext(gen))
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert closed.is_set()
