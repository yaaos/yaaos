"""Service tests: POST /api/orgs/{slug}/agents/cancel-shutdown mixed-lifecycle outcomes.

Verifies:
- draining agents return 'active' outcome, lifecycle flipped, audit written
- active/unconfigured agents return 'not_draining'
- shutdown agents return 'already_shutdown'
- mixed selection returns per-row outcomes
- cross-org agent_id returns 'not_found'
- unknown agent_id returns 'not_found'
- empty list → 400
- 101 agents → 400
- duplicate ids → 400
- non-admin → 403
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

import app.core.sessions  # side-effect: triggers auth route registration
import app.domain.orgs  # noqa: F401 — triggers orgs route registration
from app.core.agent_gateway.models import WorkspaceAgentRow

# ── App / client helpers ────────────────────────────────────────────────────


def _app() -> FastAPI:
    from app.core.auth import AuthMiddleware  # noqa: PLC0415
    from app.core.webserver import mount_specs  # noqa: PLC0415

    _app = FastAPI()
    _app.add_middleware(AuthMiddleware)
    mount_specs(_app, only={"orgs"})
    return _app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


async def _make_session(db_session, *, role_str: str = "admin") -> tuple[UUID, str, str, str]:
    from app.core.auth import Role  # noqa: PLC0415
    from app.core.identity import create_user, mint_session  # noqa: PLC0415
    from app.domain.orgs import insert_membership, insert_org  # noqa: PLC0415

    role = Role(role_str)
    slug = f"cancel-sd-{uuid4().hex[:6]}"
    org = await insert_org(db_session, slug=slug)
    user = await create_user(db_session, display_name="Test User")
    await insert_membership(db_session, user_id=user.id, org_id=org.org_id, role=role, handle="tuser")
    sess = await mint_session(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()
    return org.org_id, slug, sess.raw_token, sess.csrf_token


async def _insert_agent(db_session, *, org_id: UUID, lifecycle: str) -> WorkspaceAgentRow:
    row = WorkspaceAgentRow(
        org_id=org_id,
        instance_id=f"test-csd-{uuid4().hex[:8]}",
        iam_arn=f"arn:aws:iam::123456789012:role/test-{uuid4().hex[:6]}",
        version="0.0.1",
        state="reachable",
        lifecycle=lifecycle,
        claimed_workspace_count=0,
        last_heartbeat_at=datetime.now(UTC),
    )
    db_session.add(row)
    await db_session.flush()
    return row


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_cancel_shutdown_draining_returns_active(db_session) -> None:
    """Draining agent → lifecycle flips to active, outcome='active'."""
    org_id, slug, cookie, csrf = await _make_session(db_session, role_str="admin")
    agent = await _insert_agent(db_session, org_id=org_id, lifecycle="draining")
    await db_session.commit()

    async with _client() as c:
        resp = await c.post(
            f"/api/orgs/{slug}/agents/cancel-shutdown",
            json={"agent_ids": [str(agent.id)]},
            headers={"X-Yaaos-Org-Slug": slug, "X-CSRF-Token": csrf},
            cookies={"yaaos_session": cookie, "yaaos_csrf": csrf},
        )
    assert resp.status_code == 200, resp.text
    results = {r["agent_id"]: r["outcome"] for r in resp.json()["results"]}
    assert results[str(agent.id)] == "active"

    await db_session.refresh(agent)
    assert agent.lifecycle == "active"


@pytest.mark.asyncio
@pytest.mark.service
async def test_cancel_shutdown_draining_writes_audit(db_session) -> None:
    """CAS-win writes workspace_agent.cancel_shutdown_requested audit."""
    from app.core.audit_log import list_for_entity  # noqa: PLC0415

    org_id, slug, cookie, csrf = await _make_session(db_session, role_str="admin")
    agent = await _insert_agent(db_session, org_id=org_id, lifecycle="draining")
    await db_session.commit()

    async with _client() as c:
        await c.post(
            f"/api/orgs/{slug}/agents/cancel-shutdown",
            json={"agent_ids": [str(agent.id)]},
            headers={"X-Yaaos-Org-Slug": slug, "X-CSRF-Token": csrf},
            cookies={"yaaos_session": cookie, "yaaos_csrf": csrf},
        )

    entries = await list_for_entity("workspace_agent", agent.id, org_id=org_id)
    kinds = [e.kind for e in entries]
    assert "workspace_agent.cancel_shutdown_requested" in kinds


@pytest.mark.asyncio
@pytest.mark.service
async def test_cancel_shutdown_active_returns_not_draining(db_session) -> None:
    """Active agent → not draining; lifecycle unchanged."""
    org_id, slug, cookie, csrf = await _make_session(db_session, role_str="admin")
    agent = await _insert_agent(db_session, org_id=org_id, lifecycle="active")
    await db_session.commit()

    async with _client() as c:
        resp = await c.post(
            f"/api/orgs/{slug}/agents/cancel-shutdown",
            json={"agent_ids": [str(agent.id)]},
            headers={"X-Yaaos-Org-Slug": slug, "X-CSRF-Token": csrf},
            cookies={"yaaos_session": cookie, "yaaos_csrf": csrf},
        )
    assert resp.status_code == 200
    results = {r["agent_id"]: r["outcome"] for r in resp.json()["results"]}
    assert results[str(agent.id)] == "not_draining"


@pytest.mark.asyncio
@pytest.mark.service
async def test_cancel_shutdown_unconfigured_returns_not_draining(db_session) -> None:
    """Unconfigured agent → not draining."""
    org_id, slug, cookie, csrf = await _make_session(db_session, role_str="admin")
    agent = await _insert_agent(db_session, org_id=org_id, lifecycle="unconfigured")
    await db_session.commit()

    async with _client() as c:
        resp = await c.post(
            f"/api/orgs/{slug}/agents/cancel-shutdown",
            json={"agent_ids": [str(agent.id)]},
            headers={"X-Yaaos-Org-Slug": slug, "X-CSRF-Token": csrf},
            cookies={"yaaos_session": cookie, "yaaos_csrf": csrf},
        )
    assert resp.status_code == 200
    results = {r["agent_id"]: r["outcome"] for r in resp.json()["results"]}
    assert results[str(agent.id)] == "not_draining"


@pytest.mark.asyncio
@pytest.mark.service
async def test_cancel_shutdown_shutdown_returns_already_shutdown(db_session) -> None:
    """Already-shutdown agent → already_shutdown, lifecycle unchanged."""
    org_id, slug, cookie, csrf = await _make_session(db_session, role_str="admin")
    agent = await _insert_agent(db_session, org_id=org_id, lifecycle="shutdown")
    await db_session.commit()

    async with _client() as c:
        resp = await c.post(
            f"/api/orgs/{slug}/agents/cancel-shutdown",
            json={"agent_ids": [str(agent.id)]},
            headers={"X-Yaaos-Org-Slug": slug, "X-CSRF-Token": csrf},
            cookies={"yaaos_session": cookie, "yaaos_csrf": csrf},
        )
    assert resp.status_code == 200
    results = {r["agent_id"]: r["outcome"] for r in resp.json()["results"]}
    assert results[str(agent.id)] == "already_shutdown"


@pytest.mark.asyncio
@pytest.mark.service
async def test_cancel_shutdown_mixed_selection(db_session) -> None:
    """Mixed selection returns per-row outcomes without rejecting the bulk."""
    org_id, slug, cookie, csrf = await _make_session(db_session, role_str="admin")
    a_draining = await _insert_agent(db_session, org_id=org_id, lifecycle="draining")
    a_active = await _insert_agent(db_session, org_id=org_id, lifecycle="active")
    a_shutdown = await _insert_agent(db_session, org_id=org_id, lifecycle="shutdown")
    unknown_id = uuid4()
    await db_session.commit()

    async with _client() as c:
        resp = await c.post(
            f"/api/orgs/{slug}/agents/cancel-shutdown",
            json={
                "agent_ids": [
                    str(a_draining.id),
                    str(a_active.id),
                    str(a_shutdown.id),
                    str(unknown_id),
                ]
            },
            headers={"X-Yaaos-Org-Slug": slug, "X-CSRF-Token": csrf},
            cookies={"yaaos_session": cookie, "yaaos_csrf": csrf},
        )
    assert resp.status_code == 200
    results = {r["agent_id"]: r["outcome"] for r in resp.json()["results"]}
    assert results[str(a_draining.id)] == "active"
    assert results[str(a_active.id)] == "not_draining"
    assert results[str(a_shutdown.id)] == "already_shutdown"
    assert results[str(unknown_id)] == "not_found"


@pytest.mark.asyncio
@pytest.mark.service
async def test_cancel_shutdown_cross_org_returns_not_found(db_session) -> None:
    """Cross-org agent_id returns not_found; the agent's lifecycle is unchanged."""
    _org_id, slug, cookie, csrf = await _make_session(db_session, role_str="admin")

    from app.domain.orgs import insert_org  # noqa: PLC0415

    other_org = await insert_org(db_session, slug=f"cother-{uuid4().hex[:6]}")
    other_agent = await _insert_agent(db_session, org_id=other_org.org_id, lifecycle="draining")
    await db_session.commit()

    async with _client() as c:
        resp = await c.post(
            f"/api/orgs/{slug}/agents/cancel-shutdown",
            json={"agent_ids": [str(other_agent.id)]},
            headers={"X-Yaaos-Org-Slug": slug, "X-CSRF-Token": csrf},
            cookies={"yaaos_session": cookie, "yaaos_csrf": csrf},
        )
    assert resp.status_code == 200
    results = {r["agent_id"]: r["outcome"] for r in resp.json()["results"]}
    assert results[str(other_agent.id)] == "not_found"

    await db_session.refresh(other_agent)
    assert other_agent.lifecycle == "draining"  # unchanged


@pytest.mark.asyncio
@pytest.mark.service
async def test_cancel_shutdown_empty_list_400(db_session) -> None:
    _org_id, slug, cookie, csrf = await _make_session(db_session, role_str="admin")
    await db_session.commit()

    async with _client() as c:
        resp = await c.post(
            f"/api/orgs/{slug}/agents/cancel-shutdown",
            json={"agent_ids": []},
            headers={"X-Yaaos-Org-Slug": slug, "X-CSRF-Token": csrf},
            cookies={"yaaos_session": cookie, "yaaos_csrf": csrf},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
@pytest.mark.service
async def test_cancel_shutdown_101_agents_400(db_session) -> None:
    _org_id, slug, cookie, csrf = await _make_session(db_session, role_str="admin")
    await db_session.commit()

    async with _client() as c:
        resp = await c.post(
            f"/api/orgs/{slug}/agents/cancel-shutdown",
            json={"agent_ids": [str(uuid4()) for _ in range(101)]},
            headers={"X-Yaaos-Org-Slug": slug, "X-CSRF-Token": csrf},
            cookies={"yaaos_session": cookie, "yaaos_csrf": csrf},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
@pytest.mark.service
async def test_cancel_shutdown_duplicate_ids_400(db_session) -> None:
    _org_id, slug, cookie, csrf = await _make_session(db_session, role_str="admin")
    await db_session.commit()

    dup = str(uuid4())
    async with _client() as c:
        resp = await c.post(
            f"/api/orgs/{slug}/agents/cancel-shutdown",
            json={"agent_ids": [dup, dup]},
            headers={"X-Yaaos-Org-Slug": slug, "X-CSRF-Token": csrf},
            cookies={"yaaos_session": cookie, "yaaos_csrf": csrf},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
@pytest.mark.service
async def test_cancel_shutdown_non_admin_403(db_session) -> None:
    _org_id, slug, cookie, csrf = await _make_session(db_session, role_str="builder")
    await db_session.commit()

    async with _client() as c:
        resp = await c.post(
            f"/api/orgs/{slug}/agents/cancel-shutdown",
            json={"agent_ids": [str(uuid4())]},
            headers={"X-Yaaos-Org-Slug": slug, "X-CSRF-Token": csrf},
            cookies={"yaaos_session": cookie, "yaaos_csrf": csrf},
        )
    assert resp.status_code == 403
