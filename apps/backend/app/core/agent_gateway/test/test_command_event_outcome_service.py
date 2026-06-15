"""HTTP-layer service tests for the CommandEventAck outcome body on
POST /api/v1/commands/{id}/events.

The endpoint always returns 200 with `{"command_event_outcome": "<value>"}`.
These tests assert the two possible outcomes at the HTTP layer and verify that
the backend stamps `command_event.outcome` on the FastAPI request span.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid7

import httpx
import pytest
from fastapi import FastAPI

from app.core.agent_gateway import (
    AuthBlock,
    ProvisionWorkspaceCommand,
    RepoRef,
    bearers,
    enqueue_command,
)
from app.core.agent_gateway.models import WorkspaceAgentRow
from app.domain.orgs import repository as orgs_repo
from app.testing.observability import span_capture
from app.testing.seed import seed_workspace

# ── App factory ───────────────────────────────────────────────────────────


def _app() -> FastAPI:
    app = FastAPI()
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"agent_gateway"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


# ── Shared setup ─────────────────────────────────────────────────────────


async def _insert_agent(db_session, org_id: UUID) -> UUID:
    agent = WorkspaceAgentRow(
        id=uuid4(),
        org_id=org_id,
        instance_id=f"test-task-{uuid4().hex[:8]}",
        iam_arn=f"arn:aws:iam::123456789012:role/test-{uuid4().hex[:6]}",
        version="0.0.1",
        state="reachable",
    )
    db_session.add(agent)
    await db_session.commit()
    return agent.id


async def _setup_agent_with_bearer(db_session):
    """Insert one org + one agent; issue a bearer. Returns (agent_id, org_id, token)."""
    org = await orgs_repo.insert_org(db_session, slug=f"outcome-{uuid4().hex[:6]}")
    org.registered_iam_arn = f"arn:aws:iam::123456789012:role/test-{uuid4().hex[:6]}"
    org.aws_region = "us-east-1"
    await db_session.commit()

    agent_id = await _insert_agent(db_session, org.org_id)
    plaintext, _ = await bearers.issue(
        agent_id=agent_id, org_id=org.org_id, session=db_session, source_ip="127.0.0.1"
    )
    await db_session.commit()
    return agent_id, org.org_id, plaintext


# ── Tests ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_command_event_stale_claim_returns_410(db_session) -> None:
    """A stale command_id (no matching agent_commands row) returns 410 with
    `{"error": "stale_claim"}` — matching the workspace-event handler shape."""
    agent_id, org_id, token = await _setup_agent_with_bearer(db_session)
    del agent_id, org_id
    cmd_id = uuid7()

    async with _client() as c:
        resp = await c.post(
            f"/api/v1/commands/{cmd_id}/events",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "command_id": str(cmd_id),
                "kind": "completed_success",
                "reported_at": datetime.now(UTC).isoformat(),
                "traceparent": "00-aabb-1122-01",
            },
        )
    assert resp.status_code == 410, resp.text
    assert resp.json()["error"] == "stale_claim"


@pytest.mark.asyncio
@pytest.mark.service
async def test_command_event_recorded_returns_200_with_outcome(db_session) -> None:
    """A valid event for an existing command returns 200 with
    `command_event_outcome = event_recorded`."""
    agent_id, org_id, token = await _setup_agent_with_bearer(db_session)
    cmd_id = uuid7()
    wfx_id = uuid4()

    ws_id = await seed_workspace(
        org_id=org_id,
        provider_id="remote_agent",
        sha="deadbeef",
        current_command_id=cmd_id,
        agent_id=agent_id,
        caller_session=db_session,
    )
    provision = ProvisionWorkspaceCommand(
        command_id=cmd_id,
        workspace_id=UUID(ws_id),
        traceparent="00-aabbccdd-1122-01",
        repo=RepoRef(
            plugin_id="github",
            external_id="123",
            clone_url="https://github.com/me/repo.git",
            head_sha="deadbeef",
        ),
        history=1,
        auth=AuthBlock(kind="github_installation", token="redacted"),
        ttl_seconds=600,
        max_idle_seconds=600,
    )
    await enqueue_command(
        org_id=org_id,
        command=provision,
        session=db_session,
        workflow_execution_id=wfx_id,
    )
    await db_session.commit()

    async with _client() as c:
        resp = await c.post(
            f"/api/v1/commands/{cmd_id}/events",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "command_id": str(cmd_id),
                "kind": "completed_success",
                "reported_at": datetime.now(UTC).isoformat(),
                "traceparent": "00-aabb-1122-01",
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"command_event_outcome": "event_recorded"}


@pytest.mark.asyncio
@pytest.mark.service
async def test_command_event_span_carries_outcome_attribute(db_session) -> None:
    """The FastAPI request span carries `command_event.outcome="event_recorded"`
    after a successful event post. Drives through the ASGI transport so the
    assertion exercises `org_context`, FastAPI DI, and the backend's
    `set_attribute` call site at `web.py`.
    """
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: PLC0415

    agent_id, org_id, token = await _setup_agent_with_bearer(db_session)
    cmd_id = uuid7()
    wfx_id = uuid4()

    ws_id = await seed_workspace(
        org_id=org_id,
        provider_id="remote_agent",
        sha="deadbeef",
        current_command_id=cmd_id,
        agent_id=agent_id,
        caller_session=db_session,
    )
    provision = ProvisionWorkspaceCommand(
        command_id=cmd_id,
        workspace_id=UUID(ws_id),
        traceparent="00-aabbccdd-1122-01",
        repo=RepoRef(
            plugin_id="github",
            external_id="123",
            clone_url="https://github.com/me/repo.git",
            head_sha="deadbeef",
        ),
        history=1,
        auth=AuthBlock(kind="github_installation", token="redacted"),
        ttl_seconds=600,
        max_idle_seconds=600,
    )
    await enqueue_command(
        org_id=org_id,
        command=provision,
        session=db_session,
        workflow_execution_id=wfx_id,
    )
    await db_session.commit()

    with span_capture() as exporter:
        # Per-app instrumentation against the provider span_capture() installed.
        # Service tests don't run global configure_otel(), so we instrument the
        # test app instance explicitly.
        app = _app()
        FastAPIInstrumentor.instrument_app(app)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(
                    f"/api/v1/commands/{cmd_id}/events",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "command_id": str(cmd_id),
                        "kind": "completed_success",
                        "reported_at": datetime.now(UTC).isoformat(),
                        "traceparent": "00-aabb-1122-01",
                    },
                )
        finally:
            FastAPIInstrumentor.uninstrument_app(app)

    assert resp.status_code == 200, resp.text
    spans = exporter.get_finished_spans()
    outcome_attrs = [
        dict(s.attributes or {}).get("command_event.outcome")
        for s in spans
        if dict(s.attributes or {}).get("command_event.outcome") is not None
    ]
    assert outcome_attrs, f"no span carries command_event.outcome; spans: {[s.name for s in spans]}"
    assert "event_recorded" in outcome_attrs, f"expected event_recorded in {outcome_attrs}"
