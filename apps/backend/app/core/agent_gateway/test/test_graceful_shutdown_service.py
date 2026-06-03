"""`DELETE /api/v1/agent/identity` — graceful shutdown + workspace cleanup.

Service tests for:
- DELETE revokes bearer, sets agent offline + last_shutdown_at, expires held
  Workspaces, synthesizes terminal command failures, publishes agent_liveness_changed.
- ARN change/clear in patch_org_settings calls revoke_all_for_arn on the old ARN;
  a bearer issued under the old ARN then 401s on next verify.
- Region-mismatch identity exchange writes one `identity_exchange_failed` audit
  row attributed to the org; a no-org ARN writes none.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.agent_gateway.models import WorkspaceAgentRow
from app.core.agent_gateway.sts_verifier import (
    VerifiedIdentity,
    reset_nonce_cache_for_tests,
    set_verify_identity_override,
)
from app.core.audit_log import list_for_entity
from app.core.tenancy import update_org_fields
from app.core.workspace import WorkspaceStatus, get_workspace_info
from app.domain.orgs import repository as orgs_repo
from app.testing.seed import seed_agent, seed_workspace

# ── App / client helpers ─────────────────────────────────────────────────


def _agent_app() -> FastAPI:
    app = FastAPI()
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"agent_gateway"})
    return app


def _agent_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_agent_app()), base_url="http://test")


def _orgs_app() -> FastAPI:
    # Side-effect imports: register routes before mounting specs.
    import app.core.sessions.web  # noqa: PLC0415
    import app.domain.orgs.org_settings_web  # noqa: PLC0415
    from app.core.auth import AuthMiddleware  # noqa: PLC0415
    from app.core.webserver import mount_specs  # noqa: PLC0415

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    mount_specs(app, only={"orgs"})
    return app


def _orgs_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_orgs_app()), base_url="http://test")


async def _make_admin_session(db_session):
    from app.core.auth import Role  # noqa: PLC0415
    from app.core.identity import repository as identity_repo  # noqa: PLC0415
    from app.core.identity import sessions as sess_lifecycle  # noqa: PLC0415

    org = await orgs_repo.insert_org(db_session, slug=f"shutdown-{uuid4().hex[:6]}")
    await update_org_fields(
        db_session,
        org.org_id,
        registered_iam_arn="arn:aws:iam::111122223333:role/yaaos",
        aws_region="us-east-1",
    )
    user = await identity_repo.insert_user(db_session, display_name="Admin")
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org.org_id, role=Role.ADMIN, handle="admin"
    )
    sess = await sess_lifecycle.create(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()
    # Return raw_token, csrf_token for double-submit CSRF in mutation requests.
    return org, user.id, sess.raw_token, sess.csrf_token


async def _seed_org_and_agent(db_session, *, iam_arn: str = "arn:aws:iam::111122223333:role/yaaos"):
    """Create an org row (required by bearer_tokens FK) + a seed agent. Returns (org, agent_dict)."""
    org = await orgs_repo.insert_org(db_session, slug=f"del-test-{uuid4().hex[:6]}")
    await update_org_fields(db_session, org.org_id, registered_iam_arn=iam_arn, aws_region="us-east-1")
    agent = await seed_agent(
        org_id=org.org_id,
        session=db_session,
        iam_arn=iam_arn,
        heartbeat_age_seconds=5,
    )
    return org, agent


@pytest.fixture(autouse=True)
def _reset_sts_verifier():
    reset_nonce_cache_for_tests()
    yield
    set_verify_identity_override(None)
    reset_nonce_cache_for_tests()


# ── DELETE /api/v1/agent/identity ─────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_delete_identity_revokes_bearer_and_sets_offline(db_session) -> None:
    """DELETE with a valid bearer revokes that bearer, sets agent state=offline,
    and stamps last_shutdown_at."""
    org, agent = await _seed_org_and_agent(db_session)
    await db_session.commit()

    from app.core.agent_gateway import bearers  # noqa: PLC0415
    from app.core.database import session as get_session  # noqa: PLC0415

    async with get_session() as s:
        plaintext, _record = await bearers.issue(
            agent_id=agent["id"],
            org_id=org.org_id,
            session=s,
            issued_iam_arn="arn:aws:iam::111122223333:role/yaaos",
        )
        await s.commit()

    async with _agent_client() as c:
        resp = await c.delete(
            "/api/v1/agent/identity",
            headers={"Authorization": f"Bearer {plaintext}"},
        )
    assert resp.status_code == 204, resp.text

    # Bearer must be revoked.
    verified = await bearers.verify(plaintext)
    assert verified is None

    # Agent row must be offline with last_shutdown_at set.
    from app.core.database import session as get_session2  # noqa: PLC0415

    async with get_session2() as s:
        row = (
            await s.execute(select(WorkspaceAgentRow).where(WorkspaceAgentRow.id == agent["id"]))
        ).scalar_one()
    assert row.state == "offline"
    assert row.last_shutdown_at is not None


@pytest.mark.asyncio
@pytest.mark.service
async def test_delete_identity_expires_held_workspaces_and_synthesizes_failure(db_session) -> None:
    """DELETE expires agent-owned ACTIVE workspaces and enqueues a terminal
    failure for each in-flight current_command_id."""
    org, agent = await _seed_org_and_agent(db_session)

    command_id = uuid4()
    workflow_exec_id = uuid4()

    ws_id_str = await seed_workspace(
        org_id=org.org_id,
        provider_id="remote_agent",
        plugin_state={},
        sha="abc",
        current_command_id=command_id,
        current_holder_workflow_id=workflow_exec_id,
        agent_id=agent["id"],
        caller_session=db_session,
    )
    await db_session.commit()

    from app.core.agent_gateway import bearers  # noqa: PLC0415
    from app.core.database import session as get_session  # noqa: PLC0415

    async with get_session() as s:
        plaintext, _ = await bearers.issue(
            agent_id=agent["id"],
            org_id=org.org_id,
            session=s,
            issued_iam_arn="arn:aws:iam::111122223333:role/yaaos",
        )
        await s.commit()

    async with _agent_client() as c:
        resp = await c.delete(
            "/api/v1/agent/identity",
            headers={"Authorization": f"Bearer {plaintext}"},
        )
    assert resp.status_code == 204, resp.text

    # Workspace must be EXPIRED — primary outcome of the agent-loss cleanup.
    ws_info = await get_workspace_info(UUID(ws_id_str))
    assert ws_info.status == WorkspaceStatus.EXPIRED

    # Pending task names from the outbox include handle_agent_event —
    # the terminal failure for the in-flight command was enqueued.
    from app.core.database import session as get_session2  # noqa: PLC0415
    from app.core.tasks import get_pending_task_names  # noqa: PLC0415

    async with get_session2() as s:
        pending = await get_pending_task_names(s)
    assert "workflow.handle_agent_event" in pending


@pytest.mark.asyncio
@pytest.mark.service
async def test_delete_identity_without_bearer_returns_401(db_session) -> None:
    """Missing bearer → 401; not 500."""
    _ = db_session
    async with _agent_client() as c:
        resp = await c.delete("/api/v1/agent/identity")
    assert resp.status_code == 401


# ── ARN change/clear revoke ──────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_arn_change_revokes_old_arn_bearers(db_session) -> None:
    """PATCH /api/orgs with a new ARN revokes bearers whose issued_iam_arn
    matches the old ARN. The revoked bearer subsequently 401s."""
    org, _user_id, session_token, csrf_token = await _make_admin_session(db_session)
    org_id = org.org_id
    old_arn = "arn:aws:iam::111122223333:role/yaaos"

    agent = await seed_agent(org_id=org_id, session=db_session, heartbeat_age_seconds=5)
    await db_session.commit()

    from app.core.agent_gateway import bearers  # noqa: PLC0415
    from app.core.database import session as get_session  # noqa: PLC0415

    async with get_session() as s:
        plaintext, _ = await bearers.issue(
            agent_id=agent["id"],
            org_id=org_id,
            session=s,
            issued_iam_arn=old_arn,
        )
        await s.commit()

    # Confirm bearer is valid before the ARN change.
    assert await bearers.verify(plaintext) is not None

    new_arn = "arn:aws:iam::111122223333:role/yaaos-v2"
    async with _orgs_client() as c:
        resp = await c.patch(
            "/api/orgs",
            json={"registered_iam_arn": new_arn, "aws_region": "us-east-1"},
            cookies={"yaaos_session": session_token, "yaaos_csrf": csrf_token},
            headers={"X-Org-Slug": org.slug, "X-CSRF-Token": csrf_token},
        )
    assert resp.status_code == 200, resp.text

    # Old bearer must be revoked.
    assert await bearers.verify(plaintext) is None


@pytest.mark.asyncio
@pytest.mark.service
async def test_arn_clear_revokes_old_arn_bearers(db_session) -> None:
    """PATCH /api/orgs clearing the ARN (set both to null) also revokes old-ARN bearers."""
    org, _user_id, session_token, csrf_token = await _make_admin_session(db_session)
    org_id = org.org_id
    old_arn = "arn:aws:iam::111122223333:role/yaaos"

    agent = await seed_agent(org_id=org_id, session=db_session, heartbeat_age_seconds=5)
    await db_session.commit()

    from app.core.agent_gateway import bearers  # noqa: PLC0415
    from app.core.database import session as get_session  # noqa: PLC0415

    async with get_session() as s:
        plaintext, _ = await bearers.issue(
            agent_id=agent["id"],
            org_id=org_id,
            session=s,
            issued_iam_arn=old_arn,
        )
        await s.commit()

    assert await bearers.verify(plaintext) is not None

    # Clear both ARN and region (paired constraint).
    async with _orgs_client() as c:
        resp = await c.patch(
            "/api/orgs",
            json={"registered_iam_arn": None, "aws_region": None},
            cookies={"yaaos_session": session_token, "yaaos_csrf": csrf_token},
            headers={"X-Org-Slug": org.slug, "X-CSRF-Token": csrf_token},
        )
    assert resp.status_code == 200, resp.text

    assert await bearers.verify(plaintext) is None


# ── Region-mismatch audit ─────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_region_mismatch_writes_identity_exchange_failed_audit_row(db_session) -> None:
    """A region-mismatch identity exchange writes one `identity_exchange_failed`
    audit row attributed to the matched org."""
    canonical_arn = "arn:aws:iam::999988887777:role/region-test"
    org = await orgs_repo.insert_org(db_session, slug=f"region-{uuid4().hex[:6]}")
    await update_org_fields(
        db_session,
        org.org_id,
        registered_iam_arn=canonical_arn,
        aws_region="us-east-1",
    )
    await db_session.commit()

    async def _stub(_payload: str) -> VerifiedIdentity:
        return VerifiedIdentity(canonical_arn=canonical_arn, raw_arn=canonical_arn, region="eu-west-1")

    set_verify_identity_override(_stub)

    _signed_payload = (
        '{"url":"https://sts.amazonaws.com/","headers":{"x-yaaos-audience":"app.yaaos.cloud"},"body":""}'
    )

    async with _agent_client() as c:
        resp = await c.post(
            "/api/v1/agent/identity",
            json={
                "kind": "aws-sts",
                "agent_version": "1.0.0",
                "agent_metadata": {},
                "payload": _signed_payload,
            },
        )
    assert resp.status_code == 401

    # Verify audit row via public list_for_entity.
    entries = await list_for_entity("org", org.org_id, org_id=org.org_id, kinds=["identity_exchange_failed"])
    assert len(entries) == 1
    assert entries[0].payload["category"] == "region_mismatch"
    assert entries[0].payload["attempted_arn"] == canonical_arn


@pytest.mark.asyncio
@pytest.mark.service
async def test_region_mismatch_no_org_writes_no_audit_row(db_session) -> None:
    """An ARN that matches no registered org produces no audit row —
    `audit_entries.org_id` is mandatory and can't be populated."""
    # Use a different unregistered ARN to avoid cross-test pollution.
    unregistered_arn = f"arn:aws:iam::000000000000:role/no-org-{uuid4().hex[:6]}"
    org = await orgs_repo.insert_org(db_session, slug=f"no-org-{uuid4().hex[:6]}")
    await db_session.commit()

    async def _stub(_payload: str) -> VerifiedIdentity:
        return VerifiedIdentity(canonical_arn=unregistered_arn, raw_arn=unregistered_arn, region="eu-west-1")

    set_verify_identity_override(_stub)

    _signed_payload = (
        '{"url":"https://sts.amazonaws.com/","headers":{"x-yaaos-audience":"app.yaaos.cloud"},"body":""}'
    )

    async with _agent_client() as c:
        resp = await c.post(
            "/api/v1/agent/identity",
            json={
                "kind": "aws-sts",
                "agent_version": "1.0.0",
                "agent_metadata": {},
                "payload": _signed_payload,
            },
        )
    # No org → 403 (unregistered ARN)
    assert resp.status_code == 403

    # No audit row written for this org (no-org failure).
    entries = await list_for_entity("org", org.org_id, org_id=org.org_id, kinds=["identity_exchange_failed"])
    assert len(entries) == 0
