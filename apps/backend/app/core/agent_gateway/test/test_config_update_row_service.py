"""Service tests for row-backed ConfigUpdate flow.

Verifies:
- Identity exchange enqueues a ConfigUpdate row alongside ensure_agent_row in
  the same transaction.
- claim_next with lifecycle="unconfigured" claims the ConfigUpdate row (not a
  ProvisionWorkspace that may also be pending).
- claim_next with lifecycle="configured" returns ProvisionWorkspace when both
  kinds are pending (ConfigUpdate is invisible in the configured path).
- Duplicate enqueues (identity-exchange retries) produce two separate rows that
  are both claimed and acked successfully in FIFO order.
- POST /commands/{id}/events returns 410 with {"error":"stale_claim"} when the
  command_id has no matching agent_commands row.
- _build_config_update is not importable (regression guard against restoration).
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid7

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.agent_gateway import bearers, enqueue_command
from app.core.agent_gateway.models import AgentCommandRow, WorkspaceAgentRow
from app.core.agent_gateway.service import (
    claim_next,
    enqueue_config_update_for_agent,
)
from app.core.agent_gateway.sts_verifier import (
    VerifiedIdentity,
    reset_nonce_cache_for_tests,
    set_verify_identity_override,
)
from app.core.agent_gateway.types import (
    AgentCommandKind,
    AuthBlock,
    ProvisionWorkspaceCommand,
    RepoRef,
)
from app.core.tenancy import update_org_fields
from app.domain.orgs import repository as orgs_repo
from app.testing.seed import seed_agent

_IDENTITY_ENDPOINT = "/api/v1/agent/identity"
_SIGNED_PAYLOAD = (
    '{"url":"https://sts.amazonaws.com/","headers":{"x-yaaos-audience":"app.yaaos.dev"},"body":""}'
)

# Per-test unique IP counter to avoid rate-limit collisions across tests.
_ip_counter = itertools.count(1)


def _unique_ip() -> str:
    n = next(_ip_counter)
    return f"10.{(n >> 16) & 0xFF}.{(n >> 8) & 0xFF}.{n & 0xFF}"


def _verified(canonical_arn: str, region: str = "us-east-1", raw_arn: str | None = None) -> VerifiedIdentity:
    return VerifiedIdentity(canonical_arn=canonical_arn, raw_arn=raw_arn or canonical_arn, region=region)


def _app() -> FastAPI:
    app = FastAPI()
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"agent_gateway"})
    return app


def _client(ip: str | None = None) -> httpx.AsyncClient:
    host = ip or _unique_ip()
    transport = httpx.ASGITransport(app=_app(), client=(host, 12345))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _make_agent(db_session, *, org_id: UUID | None = None) -> UUID:
    result = await seed_agent(org_id=org_id or uuid4(), session=db_session)
    return UUID(str(result["id"]))


def _make_provision_cmd(org_id: UUID, workspace_id: UUID | None = None) -> ProvisionWorkspaceCommand:
    del org_id  # caller uses separately
    return ProvisionWorkspaceCommand(
        command_id=uuid7(),
        workspace_id=workspace_id or uuid7(),
        traceparent="00-aabbccdd-1122-01",
        repo=RepoRef(
            plugin_id="github",
            external_id="123",
            clone_url="https://github.com/me/repo.git",
            head_sha="deadbeef",
        ),
        history=1,
        auth=AuthBlock(kind="github_installation", token="tok"),
        ttl_seconds=600,
        max_idle_seconds=600,
    )


@pytest.fixture(autouse=True)
def _reset_sts_verifier():
    reset_nonce_cache_for_tests()
    yield
    set_verify_identity_override(None)
    reset_nonce_cache_for_tests()


# ── Tests ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_identity_exchange_enqueues_config_update_row(db_session) -> None:
    """POST /api/v1/agent/identity → one ConfigUpdate row in agent_commands with
    status='pending', non-null completion_token_hash, scoped to the new agent_id."""
    canonical_arn = "arn:aws:iam::123456789012:role/yaaos-agent-cu"
    raw_arn = "arn:aws:sts::123456789012:assumed-role/yaaos-agent-cu/task-cu-01"
    org = await orgs_repo.insert_org(db_session, slug=f"cu-{uuid4().hex[:6]}")
    await update_org_fields(
        db_session,
        org.org_id,
        registered_iam_arn=canonical_arn,
        aws_region="us-east-1",
    )
    await db_session.commit()

    async def _stub(_payload: str) -> VerifiedIdentity:
        return _verified(canonical_arn, raw_arn=raw_arn)

    set_verify_identity_override(_stub)

    async with _client() as c:
        resp = await c.post(
            _IDENTITY_ENDPOINT,
            json={
                "kind": "aws-sts",
                "agent_version": "1.0.0",
                "payload": _SIGNED_PAYLOAD,
            },
        )
    assert resp.status_code == 200, resp.text
    agent_id = UUID(resp.json()["agent_id"])

    # Exactly one ConfigUpdate row must exist for the new agent_id.
    rows = (
        (
            await db_session.execute(
                select(AgentCommandRow).where(
                    AgentCommandRow.agent_id == agent_id,
                    AgentCommandRow.command_kind == AgentCommandKind.CONFIG_UPDATE,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "pending"
    assert row.completion_token_hash is not None, (
        "enqueued ConfigUpdate row must have a completion_token_hash"
    )


@pytest.mark.asyncio
@pytest.mark.service
async def test_claim_next_unconfigured_returns_row_backed_config_update(db_session) -> None:
    """lifecycle='unconfigured' claim returns the ConfigUpdate row (not a
    ProvisionWorkspace even when new_workspaces > 0)."""
    org_id = uuid4()
    agent_id = await _make_agent(db_session, org_id=org_id)
    provision_cmd = _make_provision_cmd(org_id)
    await enqueue_command(org_id=org_id, command=provision_cmd, session=db_session)
    await enqueue_config_update_for_agent(agent_id, org_id=org_id, session=db_session)
    await db_session.flush()

    claimed = await claim_next(
        agent_id,
        lifecycle="unconfigured",
        new_workspaces=4,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert claimed is not None
    assert claimed.kind == AgentCommandKind.CONFIG_UPDATE
    assert claimed.completion_token is not None

    # The ProvisionWorkspace must remain pending.
    prow = (
        await db_session.execute(
            select(AgentCommandRow).where(AgentCommandRow.id == provision_cmd.command_id)
        )
    ).scalar_one_or_none()
    assert prow is not None
    assert prow.status == "pending"


@pytest.mark.asyncio
@pytest.mark.service
async def test_claim_next_configured_returns_workspace_command_when_both_pending(db_session) -> None:
    """lifecycle='configured' claim returns ProvisionWorkspace even when a
    ConfigUpdate row is also pending (ConfigUpdate is not in the configured path)."""
    org_id = uuid4()
    agent_id = await _make_agent(db_session, org_id=org_id)
    provision_cmd = _make_provision_cmd(org_id)
    await enqueue_command(org_id=org_id, command=provision_cmd, session=db_session)
    await enqueue_config_update_for_agent(agent_id, org_id=org_id, session=db_session)
    await db_session.flush()

    claimed = await claim_next(
        agent_id,
        lifecycle="configured",
        new_workspaces=4,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert claimed is not None
    assert claimed.kind == AgentCommandKind.PROVISION_WORKSPACE
    assert claimed.command_id == provision_cmd.command_id


@pytest.mark.asyncio
@pytest.mark.service
async def test_duplicate_enqueue_both_claimed_idempotently(db_session) -> None:
    """Two enqueue_config_update_for_agent calls produce two separate rows that
    are both claimed in FIFO order and their terminal events ack 200."""
    org = await orgs_repo.insert_org(db_session, slug=f"dup-{uuid4().hex[:6]}")
    org_id = org.org_id
    agent_id = await _make_agent(db_session, org_id=org_id)

    await enqueue_config_update_for_agent(agent_id, org_id=org_id, session=db_session)
    await enqueue_config_update_for_agent(agent_id, org_id=org_id, session=db_session)
    await db_session.flush()

    # Verify two distinct rows exist.
    rows = (
        (
            await db_session.execute(
                select(AgentCommandRow)
                .where(
                    AgentCommandRow.agent_id == agent_id,
                    AgentCommandRow.command_kind == AgentCommandKind.CONFIG_UPDATE,
                )
                .order_by(AgentCommandRow.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert rows[0].id != rows[1].id

    # Claim first row and record its terminal event.
    first = await claim_next(
        agent_id,
        lifecycle="unconfigured",
        new_workspaces=0,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert first is not None
    assert first.kind == AgentCommandKind.CONFIG_UPDATE
    first_token = first.completion_token
    assert first_token

    # Issue a bearer for this agent so we can post HTTP events.
    plaintext, _ = await bearers.issue(
        agent_id=agent_id, org_id=org_id, session=db_session, source_ip="127.0.0.1"
    )
    await db_session.commit()

    async with _client() as c:
        resp1 = await c.post(
            f"/api/v1/commands/{first.command_id}/events",
            headers={"Authorization": f"Bearer {plaintext}"},
            json={
                "command_id": str(first.command_id),
                "kind": "completed_success",
                "completion_token": first_token,
                "reported_at": datetime.now(UTC).isoformat(),
                "traceparent": "00-aabb-1122-01",
            },
        )
    assert resp1.status_code == 200, resp1.text
    assert resp1.json() == {"command_event_outcome": "event_recorded"}

    # Claim second row and record its terminal event.
    second = await claim_next(
        agent_id,
        lifecycle="unconfigured",
        new_workspaces=0,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert second is not None
    assert second.kind == AgentCommandKind.CONFIG_UPDATE
    second_token = second.completion_token
    assert second_token
    assert second.command_id != first.command_id

    # command_id is the agent_commands PK and the FIFO claim sort key
    # (claim_next orders by id). It must be a time-ordered UUIDv7 so claim order
    # matches enqueue order; a random uuid4 would scramble FIFO delivery.
    assert first.command_id.version == 7
    assert second.command_id.version == 7

    await db_session.commit()

    async with _client() as c:
        resp2 = await c.post(
            f"/api/v1/commands/{second.command_id}/events",
            headers={"Authorization": f"Bearer {plaintext}"},
            json={
                "command_id": str(second.command_id),
                "kind": "completed_success",
                "completion_token": second_token,
                "reported_at": datetime.now(UTC).isoformat(),
                "traceparent": "00-aabb-1122-01",
            },
        )
    assert resp2.status_code == 200, resp2.text
    assert resp2.json() == {"command_event_outcome": "event_recorded"}


@pytest.mark.asyncio
@pytest.mark.service
async def test_events_handler_returns_410_on_missing_row(db_session) -> None:
    """POST /api/v1/commands/{random-uuid}/events with kind=completed_success
    returns 410 with {"error": "stale_claim"} when the command_id has no row."""
    org = await orgs_repo.insert_org(db_session, slug=f"sc-{uuid4().hex[:6]}")
    agent = WorkspaceAgentRow(
        id=uuid4(),
        org_id=org.org_id,
        instance_id=f"task-{uuid4().hex[:8]}",
        iam_arn=f"arn:aws:iam::123456789012:role/test-{uuid4().hex[:6]}",
        version="0.0.1",
        state="reachable",
    )
    db_session.add(agent)
    await db_session.flush()
    plaintext, _ = await bearers.issue(
        agent_id=agent.id,
        org_id=org.org_id,
        session=db_session,
        source_ip="127.0.0.1",
    )
    await db_session.commit()

    cmd_id = uuid4()
    async with _client() as c:
        resp = await c.post(
            f"/api/v1/commands/{cmd_id}/events",
            headers={"Authorization": f"Bearer {plaintext}"},
            json={
                "command_id": str(cmd_id),
                "kind": "completed_success",
                "reported_at": datetime.now(UTC).isoformat(),
                "traceparent": "00-aabb-1122-01",
            },
        )
    assert resp.status_code == 410, resp.text
    body = resp.json()
    assert body["error"] == "stale_claim"
    assert "detail" in body


@pytest.mark.asyncio
@pytest.mark.service
async def test_build_config_update_function_removed(_db_session=None) -> None:
    """_build_config_update must not be importable from service.py — regression
    guard against accidental restoration of the synthesize-on-demand path."""
    import importlib  # noqa: PLC0415

    service = importlib.import_module("app.core.agent_gateway.service")
    assert not hasattr(service, "_build_config_update"), (
        "_build_config_update was restored in service.py; it must stay deleted"
    )


@pytest.mark.asyncio
@pytest.mark.service
async def test_agent_command_pk_rejects_non_v7_uuid(db_session) -> None:
    """The ck_agent_commands_id_uuidv7 CHECK rejects an INSERT whose PK is a
    uuid4 — the authoritative guard for the FIFO sort-key invariant that the
    semgrep taint rule cannot see across the producer-DTO → enqueue_command hop.
    A uuid7 PK inserts cleanly."""
    org = await orgs_repo.insert_org(db_session, slug=f"v7-{uuid4().hex[:6]}")

    db_session.add(AgentCommandRow(id=uuid4(), org_id=org.org_id, command_kind="ProvisionWorkspace"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    # A uuid7 PK satisfies the constraint.
    org = await orgs_repo.insert_org(db_session, slug=f"v7ok-{uuid4().hex[:6]}")
    db_session.add(AgentCommandRow(id=uuid7(), org_id=org.org_id, command_kind="ProvisionWorkspace"))
    await db_session.flush()
