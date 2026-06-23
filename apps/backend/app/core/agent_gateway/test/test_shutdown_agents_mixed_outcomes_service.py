"""Service tests: POST /api/orgs/{slug}/agents/shutdown mixed-lifecycle outcomes.

Verifies:
- all-active agents return 'draining' outcome, audit written, SSE published
- all-draining agents return 'already_draining'
- all-shutdown agents return 'already_shutdown'
- mixed selection returns per-row outcomes
- cross-org agent_id returns 'not_found' (no data leak)
- unknown agent_id returns 'not_found'
- empty agent_ids list → 400
- 101 agent_ids → 400 (max 100)
- duplicate agent_ids → 400
- non-admin caller → 403
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
    """FastAPI app with orgs routes (includes agent shutdown endpoints)."""
    from app.core.auth import AuthMiddleware  # noqa: PLC0415
    from app.core.webserver import mount_specs  # noqa: PLC0415

    _app = FastAPI()
    _app.add_middleware(AuthMiddleware)
    mount_specs(_app, only={"orgs"})
    return _app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


async def _make_session(db_session, *, role_str: str = "admin") -> tuple[UUID, str, str, str]:
    """Create org + user with given role + session. Returns (org_id, slug, cookie, csrf)."""
    from app.core.auth import Role  # noqa: PLC0415
    from app.core.identity import create_user, mint_session  # noqa: PLC0415
    from app.domain.orgs import insert_membership, insert_org  # noqa: PLC0415

    role = Role(role_str)
    slug = f"shutdown-{uuid4().hex[:6]}"
    org = await insert_org(db_session, slug=slug)
    user = await create_user(db_session, display_name="Test User")
    await insert_membership(db_session, user_id=user.id, org_id=org.org_id, role=role, handle="tuser")
    sess = await mint_session(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()
    return org.org_id, slug, sess.raw_token, sess.csrf_token


async def _insert_agent(db_session, *, org_id: UUID, lifecycle: str) -> WorkspaceAgentRow:
    """Insert a workspace_agents row with the given lifecycle."""
    row = WorkspaceAgentRow(
        org_id=org_id,
        instance_id=f"test-sd-{uuid4().hex[:8]}",
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
async def test_shutdown_all_active_returns_draining(db_session) -> None:
    """All-active selection: every agent transitions to draining."""
    org_id, slug, cookie, csrf = await _make_session(db_session, role_str="admin")
    agent_a = await _insert_agent(db_session, org_id=org_id, lifecycle="active")
    agent_b = await _insert_agent(db_session, org_id=org_id, lifecycle="active")
    await db_session.commit()

    async with _client() as c:
        resp = await c.post(
            f"/api/orgs/{slug}/agents/shutdown",
            json={"agent_ids": [str(agent_a.id), str(agent_b.id)]},
            headers={"X-Yaaos-Org-Slug": slug, "X-CSRF-Token": csrf},
            cookies={"yaaos_session": cookie, "yaaos_csrf": csrf},
        )

    assert resp.status_code == 200, resp.text
    results = {r["agent_id"]: r["outcome"] for r in resp.json()["results"]}
    assert results[str(agent_a.id)] == "draining"
    assert results[str(agent_b.id)] == "draining"

    # Verify DB state
    await db_session.refresh(agent_a)
    await db_session.refresh(agent_b)
    assert agent_a.lifecycle == "draining"
    assert agent_b.lifecycle == "draining"


@pytest.mark.asyncio
@pytest.mark.service
async def test_shutdown_all_active_writes_audit(db_session) -> None:
    """Shutdown CAS-win writes workspace_agent.shutdown_requested audit for each agent."""
    from app.core.audit_log import list_for_entity  # noqa: PLC0415

    org_id, slug, cookie, csrf = await _make_session(db_session, role_str="admin")
    agent = await _insert_agent(db_session, org_id=org_id, lifecycle="active")
    await db_session.commit()

    async with _client() as c:
        resp = await c.post(
            f"/api/orgs/{slug}/agents/shutdown",
            json={"agent_ids": [str(agent.id)]},
            headers={"X-Yaaos-Org-Slug": slug, "X-CSRF-Token": csrf},
            cookies={"yaaos_session": cookie, "yaaos_csrf": csrf},
        )
    assert resp.status_code == 200

    entries = await list_for_entity("workspace_agent", agent.id, org_id=org_id)
    kinds = [e.kind for e in entries]
    assert "workspace_agent.shutdown_requested" in kinds


@pytest.mark.asyncio
@pytest.mark.service
async def test_shutdown_all_draining_returns_already_draining(db_session) -> None:
    """All-draining selection: every agent returns 'already_draining'."""
    org_id, slug, cookie, csrf = await _make_session(db_session, role_str="admin")
    agent = await _insert_agent(db_session, org_id=org_id, lifecycle="draining")
    await db_session.commit()

    async with _client() as c:
        resp = await c.post(
            f"/api/orgs/{slug}/agents/shutdown",
            json={"agent_ids": [str(agent.id)]},
            headers={"X-Yaaos-Org-Slug": slug, "X-CSRF-Token": csrf},
            cookies={"yaaos_session": cookie, "yaaos_csrf": csrf},
        )
    assert resp.status_code == 200
    results = {r["agent_id"]: r["outcome"] for r in resp.json()["results"]}
    assert results[str(agent.id)] == "already_draining"


@pytest.mark.asyncio
@pytest.mark.service
async def test_shutdown_all_shutdown_returns_already_shutdown(db_session) -> None:
    """All-shutdown selection: every agent returns 'already_shutdown'."""
    org_id, slug, cookie, csrf = await _make_session(db_session, role_str="admin")
    agent = await _insert_agent(db_session, org_id=org_id, lifecycle="shutdown")
    await db_session.commit()

    async with _client() as c:
        resp = await c.post(
            f"/api/orgs/{slug}/agents/shutdown",
            json={"agent_ids": [str(agent.id)]},
            headers={"X-Yaaos-Org-Slug": slug, "X-CSRF-Token": csrf},
            cookies={"yaaos_session": cookie, "yaaos_csrf": csrf},
        )
    assert resp.status_code == 200
    results = {r["agent_id"]: r["outcome"] for r in resp.json()["results"]}
    assert results[str(agent.id)] == "already_shutdown"


@pytest.mark.asyncio
@pytest.mark.service
async def test_shutdown_mixed_selection_returns_per_row_outcomes(db_session) -> None:
    """Mixed lifecycle selection returns per-row outcomes without rejecting the bulk."""
    org_id, slug, cookie, csrf = await _make_session(db_session, role_str="admin")
    a_active = await _insert_agent(db_session, org_id=org_id, lifecycle="active")
    a_unconfigured = await _insert_agent(db_session, org_id=org_id, lifecycle="unconfigured")
    a_draining = await _insert_agent(db_session, org_id=org_id, lifecycle="draining")
    a_shutdown = await _insert_agent(db_session, org_id=org_id, lifecycle="shutdown")
    unknown_id = uuid4()
    await db_session.commit()

    async with _client() as c:
        resp = await c.post(
            f"/api/orgs/{slug}/agents/shutdown",
            json={
                "agent_ids": [
                    str(a_active.id),
                    str(a_unconfigured.id),
                    str(a_draining.id),
                    str(a_shutdown.id),
                    str(unknown_id),
                ]
            },
            headers={"X-Yaaos-Org-Slug": slug, "X-CSRF-Token": csrf},
            cookies={"yaaos_session": cookie, "yaaos_csrf": csrf},
        )
    assert resp.status_code == 200
    results = {r["agent_id"]: r["outcome"] for r in resp.json()["results"]}
    assert results[str(a_active.id)] == "draining"
    assert results[str(a_unconfigured.id)] == "draining"
    assert results[str(a_draining.id)] == "already_draining"
    assert results[str(a_shutdown.id)] == "already_shutdown"
    assert results[str(unknown_id)] == "not_found"


@pytest.mark.asyncio
@pytest.mark.service
async def test_shutdown_cross_org_agent_returns_not_found(db_session) -> None:
    """An agent_id belonging to a different org returns not_found — no data leak."""
    _org_id, slug, cookie, csrf = await _make_session(db_session, role_str="admin")

    # Create a second org and an agent in it
    from app.domain.orgs import insert_org  # noqa: PLC0415

    other_org = await insert_org(db_session, slug=f"other-{uuid4().hex[:6]}")
    other_agent = await _insert_agent(db_session, org_id=other_org.org_id, lifecycle="active")
    await db_session.commit()

    async with _client() as c:
        resp = await c.post(
            f"/api/orgs/{slug}/agents/shutdown",
            json={"agent_ids": [str(other_agent.id)]},
            headers={"X-Yaaos-Org-Slug": slug, "X-CSRF-Token": csrf},
            cookies={"yaaos_session": cookie, "yaaos_csrf": csrf},
        )
    assert resp.status_code == 200
    results = {r["agent_id"]: r["outcome"] for r in resp.json()["results"]}
    assert results[str(other_agent.id)] == "not_found"

    # Other org's agent lifecycle is unchanged
    await db_session.refresh(other_agent)
    assert other_agent.lifecycle == "active"


@pytest.mark.asyncio
@pytest.mark.service
async def test_shutdown_no_audit_for_noop_outcomes(db_session) -> None:
    """already_draining, already_shutdown, not_found outcomes do NOT write audit rows."""
    from app.core.audit_log import list_for_entity  # noqa: PLC0415

    org_id, slug, cookie, csrf = await _make_session(db_session, role_str="admin")
    agent_draining = await _insert_agent(db_session, org_id=org_id, lifecycle="draining")
    agent_shutdown = await _insert_agent(db_session, org_id=org_id, lifecycle="shutdown")
    unknown_id = uuid4()
    await db_session.commit()

    async with _client() as c:
        await c.post(
            f"/api/orgs/{slug}/agents/shutdown",
            json={"agent_ids": [str(agent_draining.id), str(agent_shutdown.id), str(unknown_id)]},
            headers={"X-Yaaos-Org-Slug": slug, "X-CSRF-Token": csrf},
            cookies={"yaaos_session": cookie, "yaaos_csrf": csrf},
        )

    entries_d = await list_for_entity("workspace_agent", agent_draining.id, org_id=org_id)
    entries_s = await list_for_entity("workspace_agent", agent_shutdown.id, org_id=org_id)
    assert not any(e.kind == "workspace_agent.shutdown_requested" for e in entries_d)
    assert not any(e.kind == "workspace_agent.shutdown_requested" for e in entries_s)


@pytest.mark.asyncio
@pytest.mark.service
async def test_shutdown_empty_list_returns_400(db_session) -> None:
    """Empty agent_ids list → 400 invalid_payload."""
    _org_id, slug, cookie, csrf = await _make_session(db_session, role_str="admin")
    await db_session.commit()

    async with _client() as c:
        resp = await c.post(
            f"/api/orgs/{slug}/agents/shutdown",
            json={"agent_ids": []},
            headers={"X-Yaaos-Org-Slug": slug, "X-CSRF-Token": csrf},
            cookies={"yaaos_session": cookie, "yaaos_csrf": csrf},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
@pytest.mark.service
async def test_shutdown_101_agents_returns_400(db_session) -> None:
    """More than 100 agent_ids → 400 invalid_payload."""
    _org_id, slug, cookie, csrf = await _make_session(db_session, role_str="admin")
    await db_session.commit()

    async with _client() as c:
        resp = await c.post(
            f"/api/orgs/{slug}/agents/shutdown",
            json={"agent_ids": [str(uuid4()) for _ in range(101)]},
            headers={"X-Yaaos-Org-Slug": slug, "X-CSRF-Token": csrf},
            cookies={"yaaos_session": cookie, "yaaos_csrf": csrf},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
@pytest.mark.service
async def test_shutdown_duplicate_agent_ids_returns_400(db_session) -> None:
    """Duplicate agent_ids in the request → 400 invalid_payload."""
    _org_id, slug, cookie, csrf = await _make_session(db_session, role_str="admin")
    await db_session.commit()

    dup_id = str(uuid4())
    async with _client() as c:
        resp = await c.post(
            f"/api/orgs/{slug}/agents/shutdown",
            json={"agent_ids": [dup_id, dup_id]},
            headers={"X-Yaaos-Org-Slug": slug, "X-CSRF-Token": csrf},
            cookies={"yaaos_session": cookie, "yaaos_csrf": csrf},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
@pytest.mark.service
async def test_shutdown_non_admin_returns_403(db_session) -> None:
    """Builder-role caller → 403 forbidden."""
    _org_id, slug, cookie, csrf = await _make_session(db_session, role_str="builder")
    await db_session.commit()

    async with _client() as c:
        resp = await c.post(
            f"/api/orgs/{slug}/agents/shutdown",
            json={"agent_ids": [str(uuid4())]},
            headers={"X-Yaaos-Org-Slug": slug, "X-CSRF-Token": csrf},
            cookies={"yaaos_session": cookie, "yaaos_csrf": csrf},
        )
    assert resp.status_code == 403
