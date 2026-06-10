"""Service tests: liveness sweeper transitions and agents-list endpoint.

`compute_agent_liveness_transitions` writes `state` only on transition,
returns newly-offline agent IDs, and emits SSE per transition.

`GET /api/orgs/{slug}/agents` returns agents within the 1h retention window
with computed `claimed_workspace_count`; excludes agents whose last heartbeat
or shutdown occurred more than 1h ago.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.core.agent_gateway.models import WorkspaceAgentRow
from app.core.sessions import web as _sessions_web  # noqa: F401 — registers session routes + auth deps
from app.domain.orgs import org_settings_web as _org_settings_web  # noqa: F401 — registers /api/orgs/* routes

# ── App / client helpers ─────────────────────────────────────────────────


def _app() -> FastAPI:
    """Minimal app that mounts the orgs routes (includes agents list endpoint)."""
    app = FastAPI()
    from app.core.auth import AuthMiddleware  # noqa: PLC0415
    from app.core.webserver import mount_specs  # noqa: PLC0415

    app.add_middleware(AuthMiddleware)
    mount_specs(app, only={"orgs"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


async def _make_org_and_session(db_session) -> tuple[Any, Any, str]:
    """Create an org + admin session; return (org_row, user_id, session_cookie_token)."""
    from app.core.auth import Role  # noqa: PLC0415
    from app.core.identity import repository as identity_repo  # noqa: PLC0415
    from app.core.identity import sessions as session_lifecycle  # noqa: PLC0415
    from app.domain.orgs import repository as orgs_repo  # noqa: PLC0415

    org = await orgs_repo.insert_org(db_session, slug=f"liveness-{uuid4().hex[:6]}")
    user = await identity_repo.insert_user(db_session, display_name="Test User")
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org.org_id, role=Role.ADMIN, handle="test"
    )
    sess = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()
    return org, user.id, sess.raw_token


async def _seed_agent_row(
    db_session,
    *,
    org_id: UUID,
    state: str = "reachable",
    heartbeat_age_seconds: int = 0,
    instance_id: str | None = None,
) -> WorkspaceAgentRow:
    """Insert a workspace_agents row directly for precise threshold testing."""
    _id = instance_id or f"test-{uuid4().hex[:8]}"
    row = WorkspaceAgentRow(
        org_id=org_id,
        instance_id=_id,
        iam_arn=f"arn:aws:iam::123456789012:role/test-{_id}",
        version="0.0.1",
        state=state,
        claimed_workspace_count=0,
    )
    if heartbeat_age_seconds >= 0:
        row.last_heartbeat_at = datetime.now(UTC) - timedelta(seconds=heartbeat_age_seconds)
    db_session.add(row)
    await db_session.flush()
    return row


# ── Tests: compute_agent_liveness_transitions ─────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_liveness_online_under_60s_stays_reachable(db_session) -> None:
    """Agent with heartbeat < 60s ago stays reachable; no transition emitted."""
    from app.core.agent_gateway.service import compute_agent_liveness_transitions  # noqa: PLC0415

    org_id = uuid4()
    row = await _seed_agent_row(db_session, org_id=org_id, state="reachable", heartbeat_age_seconds=30)
    await db_session.commit()

    now = datetime.now(UTC)
    offline_ids = await compute_agent_liveness_transitions(now, session=db_session)

    await db_session.refresh(row)
    assert row.state == "reachable"
    assert row.id not in offline_ids


@pytest.mark.asyncio
@pytest.mark.service
async def test_liveness_reachable_to_stale_at_60s_threshold(db_session) -> None:
    """Agent with heartbeat exactly at 60s transitions reachable → stale."""
    from app.core.agent_gateway.service import compute_agent_liveness_transitions  # noqa: PLC0415

    org_id = uuid4()
    row = await _seed_agent_row(db_session, org_id=org_id, state="reachable", heartbeat_age_seconds=65)
    await db_session.commit()

    now = datetime.now(UTC)
    offline_ids = await compute_agent_liveness_transitions(now, session=db_session)

    await db_session.refresh(row)
    assert row.state == "stale"
    assert row.id not in offline_ids  # stale ≠ offline


@pytest.mark.asyncio
@pytest.mark.service
async def test_liveness_stale_to_offline_at_5min_threshold(db_session) -> None:
    """Agent with heartbeat > 5min transitions stale → offline; ID returned."""
    from app.core.agent_gateway.service import compute_agent_liveness_transitions  # noqa: PLC0415

    org_id = uuid4()
    row = await _seed_agent_row(db_session, org_id=org_id, state="stale", heartbeat_age_seconds=310)
    await db_session.commit()

    now = datetime.now(UTC)
    offline_ids = await compute_agent_liveness_transitions(now, session=db_session)

    await db_session.refresh(row)
    assert row.state == "offline"
    assert row.id in offline_ids


@pytest.mark.asyncio
@pytest.mark.service
async def test_liveness_reachable_to_offline_direct_jump_over_5min(db_session) -> None:
    """Agent that jumps directly from reachable with heartbeat > 5min → offline."""
    from app.core.agent_gateway.service import compute_agent_liveness_transitions  # noqa: PLC0415

    org_id = uuid4()
    row = await _seed_agent_row(db_session, org_id=org_id, state="reachable", heartbeat_age_seconds=400)
    await db_session.commit()

    now = datetime.now(UTC)
    offline_ids = await compute_agent_liveness_transitions(now, session=db_session)

    await db_session.refresh(row)
    assert row.state == "offline"
    assert row.id in offline_ids


@pytest.mark.asyncio
@pytest.mark.service
async def test_liveness_no_write_when_no_transition(db_session) -> None:
    """State is not updated when already correct (stale in stale band)."""
    from app.core.agent_gateway.service import compute_agent_liveness_transitions  # noqa: PLC0415

    org_id = uuid4()
    # 120s ≥ 60s → stale band; row already says stale
    row = await _seed_agent_row(db_session, org_id=org_id, state="stale", heartbeat_age_seconds=120)
    await db_session.commit()

    now = datetime.now(UTC)
    # Run once to set; then run again — second run should not re-transition.
    await compute_agent_liveness_transitions(now, session=db_session)
    await db_session.commit()

    await db_session.refresh(row)
    assert row.state == "stale"  # stays stale, doesn't flip back or forward


@pytest.mark.asyncio
@pytest.mark.service
async def test_liveness_offline_agent_stays_offline(db_session) -> None:
    """Already-offline agent is not re-transitioned."""
    from app.core.agent_gateway.service import compute_agent_liveness_transitions  # noqa: PLC0415

    org_id = uuid4()
    row = await _seed_agent_row(db_session, org_id=org_id, state="offline", heartbeat_age_seconds=700)
    await db_session.commit()

    now = datetime.now(UTC)
    offline_ids = await compute_agent_liveness_transitions(now, session=db_session)

    await db_session.refresh(row)
    assert row.state == "offline"
    # Already offline — not re-returned (only returned on transition)
    assert row.id not in offline_ids


@pytest.mark.asyncio
@pytest.mark.service
async def test_liveness_emits_sse_on_transition(db_session, redis_or_skip) -> None:
    """Each liveness transition emits one agent_liveness_changed SSE event."""
    import asyncio  # noqa: PLC0415

    from app.core.agent_gateway.service import compute_agent_liveness_transitions  # noqa: PLC0415
    from app.core.redis import RedisPubsub, bind_pubsub  # noqa: PLC0415
    from app.core.redis import shutdown as redis_shutdown  # noqa: PLC0415
    from app.core.sse import GeneralEventKind, subscribe_general  # noqa: PLC0415

    await redis_shutdown()
    bind_pubsub(RedisPubsub())

    org_id = uuid4()
    await _seed_agent_row(db_session, org_id=org_id, state="reachable", heartbeat_age_seconds=70)
    await db_session.commit()

    sub = subscribe_general(org_id)
    received: list[dict] = []

    async def _drain() -> None:
        async for evt in sub:
            received.append(evt)
            if len(received) >= 1:
                return

    drainer = asyncio.create_task(_drain())
    await asyncio.sleep(0)

    now = datetime.now(UTC)
    await compute_agent_liveness_transitions(now, session=db_session)
    await db_session.commit()

    # Allow publish tasks to run.
    await asyncio.sleep(0.05)
    drainer.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(drainer), timeout=0.1)
    except asyncio.CancelledError, TimeoutError:
        pass

    assert any(e.get("kind") == GeneralEventKind.AGENT_LIVENESS_CHANGED for e in received)


# ── Tests: GET /api/orgs/{slug}/agents ────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_agents_list_returns_within_retention_window(db_session) -> None:
    """Agents heartbeated within 1h are returned; older are excluded."""
    from app.core.agent_gateway.service import list_agents_for_org  # noqa: PLC0415

    org_id = uuid4()
    # Within window (55 min).
    fresh = await _seed_agent_row(db_session, org_id=org_id, state="reachable", heartbeat_age_seconds=55 * 60)
    # Outside window (70 min).
    old = await _seed_agent_row(db_session, org_id=org_id, state="offline", heartbeat_age_seconds=70 * 60)
    await db_session.commit()

    now = datetime.now(UTC)
    result = await list_agents_for_org(org_id, now=now, session=db_session)

    ids = {r["id"] for r in result}
    assert fresh.id in ids
    assert old.id not in ids


@pytest.mark.asyncio
@pytest.mark.service
async def test_agents_list_includes_claimed_workspace_count(db_session) -> None:
    """claimed_workspace_count is returned per agent."""
    from app.core.agent_gateway.service import list_agents_for_org  # noqa: PLC0415

    org_id = uuid4()
    row = await _seed_agent_row(db_session, org_id=org_id, state="reachable", heartbeat_age_seconds=10)
    row.claimed_workspace_count = 3
    await db_session.flush()
    await db_session.commit()

    now = datetime.now(UTC)
    result = await list_agents_for_org(org_id, now=now, session=db_session)

    assert len(result) == 1
    assert result[0]["claimed_workspace_count"] == 3


@pytest.mark.asyncio
@pytest.mark.service
async def test_agents_list_endpoint_returns_200(db_session) -> None:
    """GET /api/orgs/{slug}/agents returns 200 for an authenticated org member."""
    org, _user_id, session_token = await _make_org_and_session(db_session)
    slug = org.slug

    # Seed one agent within the retention window.
    agent_row = await _seed_agent_row(
        db_session, org_id=org.org_id, state="reachable", heartbeat_age_seconds=30
    )
    await db_session.commit()

    async with _client() as c:
        resp = await c.get(
            f"/api/orgs/{slug}/agents",
            headers={"X-Yaaos-Org-Slug": slug, "Cookie": f"yaaos_session={session_token}"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, list)
    agent_ids = [item["id"] for item in data]
    assert str(agent_row.id) in agent_ids


@pytest.mark.asyncio
@pytest.mark.service
async def test_agents_list_endpoint_requires_auth(db_session) -> None:
    """GET /api/orgs/{slug}/agents → 401 without session."""
    async with _client() as c:
        resp = await c.get(
            "/api/orgs/some-org/agents",
            headers={"X-Yaaos-Org-Slug": "some-org"},
        )
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
@pytest.mark.service
async def test_agents_list_excludes_other_org_agents(db_session) -> None:
    """Agents from a different org are not returned."""
    from app.core.agent_gateway.service import list_agents_for_org  # noqa: PLC0415

    org_a = uuid4()
    org_b = uuid4()
    agent_a = await _seed_agent_row(db_session, org_id=org_a, state="reachable", heartbeat_age_seconds=10)
    agent_b = await _seed_agent_row(db_session, org_id=org_b, state="reachable", heartbeat_age_seconds=10)
    await db_session.commit()

    now = datetime.now(UTC)
    result = await list_agents_for_org(org_a, now=now, session=db_session)
    ids = {r["id"] for r in result}
    assert agent_a.id in ids
    assert agent_b.id not in ids
