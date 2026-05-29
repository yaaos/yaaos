"""Service tests for GET /api/sse/general — auth gate + cross-org isolation.

Tests the full auth-chain contract end-to-end: 401 without session,
400 without X-Org-Slug, 403 for non-member, and org-scoped stream
isolation (org-A subscriber must not receive org-B events).

Note on the streaming test: httpx-ASGITransport hangs on close for
indefinite streams (same constraint as the workspace-status SSE test).
The cross-org isolation test therefore drives `_general_stream` directly —
the HTTP wiring for the streaming body is a thin wrapper with no logic of
its own, so this is the correct testing shape.
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
from app.core.sse import GeneralEventKind, publish_general
from app.core.sse.web import _general_stream
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
async def seeded(db_session) -> AsyncIterator[dict[str, object]]:
    """One owner user with a valid session, belonging to two orgs.
    Used for the membership-gated + cross-org tests."""
    user = await identity_repo.insert_user(db_session, display_name="TestOwner")

    org_a = await orgs_repo.insert_org(db_session, slug=f"org-a-{uuid.uuid4().hex[:8]}")
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org_a.org_id, role=Role.OWNER, handle="owner-a"
    )

    org_b = await orgs_repo.insert_org(db_session, slug=f"org-b-{uuid.uuid4().hex[:8]}")
    # User is NOT a member of org_b — used for the 403 test.

    raw_token = f"sse-test-{uuid.uuid4().hex[:8]}"
    await identity_repo.insert_session(
        db_session,
        token_hash=identity_repo.hash_token(raw_token),
        user_id=user.id,
        workspace_id=None,
        csrf_token="csrf-sse",
        ip=None,
        user_agent=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await db_session.commit()
    yield {"org_a": org_a, "org_b": org_b, "user": user, "token": raw_token}


@pytest.mark.service
@pytest.mark.asyncio
async def test_general_endpoint_returns_401_without_session() -> None:
    """`GET /api/sse/general` with no session cookie → 401."""
    async with _client() as c:
        resp = await c.get(
            "/api/sse/general",
            headers={"X-Org-Slug": "any-org"},
        )
    assert resp.status_code == 401


@pytest.mark.service
@pytest.mark.asyncio
async def test_general_endpoint_returns_400_without_org_slug(db_session) -> None:
    """`GET /api/sse/general` with session but no X-Org-Slug → 400."""
    user = await identity_repo.insert_user(db_session, display_name="NoOrgUser")
    raw_token = f"noorg-{uuid.uuid4().hex[:8]}"
    await identity_repo.insert_session(
        db_session,
        token_hash=identity_repo.hash_token(raw_token),
        user_id=user.id,
        workspace_id=None,
        csrf_token="csrf-noorg",
        ip=None,
        user_agent=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await db_session.commit()

    async with _client() as c:
        resp = await c.get(
            "/api/sse/general",
            cookies={"yaaos_session": raw_token},
        )
    assert resp.status_code == 400


@pytest.mark.service
@pytest.mark.asyncio
async def test_general_endpoint_returns_403_for_non_member(seeded) -> None:
    """Session valid, X-Org-Slug for org the user is not a member of → 403 (or 404 mask)."""
    async with _client() as c:
        resp = await c.get(
            "/api/sse/general",
            cookies={"yaaos_session": seeded["token"]},
            headers={"X-Org-Slug": seeded["org_b"].slug},
        )
    # require() masks org existence as 404 when membership is absent.
    assert resp.status_code in (403, 404)


@pytest.mark.service
@pytest.mark.asyncio
async def test_general_endpoint_streams_org_scoped_events(seeded, redis_or_skip) -> None:
    """Org-A subscriber receives only org-A events; org-B events are not delivered.

    Drives `_general_stream` directly — httpx-ASGITransport hangs on close for
    infinite streams so the HTTP wrapper is untestable end-to-end here. The auth
    gate is already covered by the three tests above; this test owns the
    cross-org isolation invariant on the generator that the route wraps.
    """
    org_a_id: uuid.UUID = seeded["org_a"].org_id
    org_b_id: uuid.UUID = seeded["org_b"].org_id

    gen = _general_stream(org_a_id)
    collector = asyncio.create_task(gen.__anext__())
    # Yield control so the generator registers its Redis subscription before
    # we publish — Redis pub/sub is fire-and-forget; earlier publishes drop.
    await asyncio.sleep(0.1)

    # Publish to org B first (must not reach org-A subscriber).
    await publish_general(
        org_id=org_b_id,
        kind=GeneralEventKind.REVIEW_STARTED,
        payload={"review_job_id": str(uuid.uuid4())},
    )

    # Publish to org A (must reach subscriber).
    ticket_id = str(uuid.uuid4())
    await publish_general(
        org_id=org_a_id,
        kind=GeneralEventKind.TICKET_STATUS_CHANGED,
        payload={"ticket_id": ticket_id},
    )

    frame: str = await asyncio.wait_for(collector, timeout=3.0)
    await gen.aclose()

    assert frame.startswith("data: "), f"SSE frame must start with 'data: '; got {frame!r}"
    assert "ticket_status_changed" in frame, f"org-A kind not in frame: {frame!r}"
    assert ticket_id in frame, f"org-A payload not in frame: {frame!r}"
    assert "review_started" not in frame, f"org-B event leaked into org-A stream: {frame!r}"
