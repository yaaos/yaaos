"""`/v1/identity/exchange` — verified ARN → org match → ensure_agent_row.

Service test against the assembled FastAPI app. The STS verifier itself
is unit-tested in `test_sts_verifier.py`; here we test the endpoint's
wiring of verifier + org-by-ARN lookup + agent-row persistence + 401/403
error mapping.

Tests use `set_verify_identity_override` to swap the production verifier
for a synchronous stub so no httpx machinery is needed.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.agent_gateway.models import WorkspaceAgentRow
from app.core.agent_gateway.sts_verifier import (
    InvalidSignedRequestError,
    set_verify_identity_override,
)
from app.domain.orgs import repository as orgs_repo


def _app() -> FastAPI:
    from app.core.webserver.registry import _specs  # noqa: PLC0415

    app = FastAPI()
    spec = _specs["agent_gateway"]
    app.include_router(spec.router, prefix=spec.url_prefix or "/api/v1")
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest.fixture(autouse=True)
def _reset_verifier():
    yield
    set_verify_identity_override(None)


async def test_identity_exchange_happy_path_persists_agent_row(db_session) -> None:
    """Valid signed_request → verifier returns ARN → ARN matches a
    registered org → workspace_agents row inserted + bearer returned."""
    arn = "arn:aws:sts::123456789012:assumed-role/yaaos-agent/task-abc"
    org = await orgs_repo.insert_org(db_session, slug=f"sts-{uuid4().hex[:6]}")
    org.registered_iam_arn = arn
    await db_session.commit()

    async def _stub(_payload: str) -> str:
        return arn

    set_verify_identity_override(_stub)

    pod_id = uuid4()
    async with _client() as c:
        resp = await c.post(
            "/api/v1/identity/exchange",
            json={
                "agent_pod_id": str(pod_id),
                "version": "1.2.3",
                "signed_request": '{"url":"https://sts.amazonaws.com/","headers":{},"body":""}',
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bearer"].startswith("placeholder-")
    assert body["agent_id"]

    # workspace_agents row persisted with the verified ARN.
    rows = (
        (await db_session.execute(select(WorkspaceAgentRow).where(WorkspaceAgentRow.org_id == org.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].iam_arn == arn
    assert rows[0].agent_pod_id == pod_id


async def test_identity_exchange_unregistered_arn_returns_403(db_session) -> None:
    """Verifier returns an ARN; no org has that ARN registered → 403."""
    arn = "arn:aws:sts::999999999999:assumed-role/unregistered/task"

    async def _stub(_payload: str) -> str:
        return arn

    set_verify_identity_override(_stub)

    async with _client() as c:
        resp = await c.post(
            "/api/v1/identity/exchange",
            json={
                "agent_pod_id": str(uuid4()),
                "version": "0.0.1",
                "signed_request": '{"url":"https://sts.amazonaws.com/","headers":{},"body":""}',
            },
        )
    assert resp.status_code == 403
    assert resp.json()["detail"]["detail"] == "forbidden_unregistered_arn"
    # No workspace_agents row persisted on the rejection path.
    rows = (await db_session.execute(select(WorkspaceAgentRow))).scalars().all()
    assert rows == []


async def test_identity_exchange_invalid_signature_returns_401(db_session) -> None:
    """Verifier raises InvalidSignedRequestError → 401."""
    del db_session

    async def _stub(_payload: str) -> str:
        raise InvalidSignedRequestError("forged signature")

    set_verify_identity_override(_stub)

    async with _client() as c:
        resp = await c.post(
            "/api/v1/identity/exchange",
            json={
                "agent_pod_id": str(uuid4()),
                "version": "0.0.1",
                "signed_request": '{"url":"https://sts.amazonaws.com/","headers":{},"body":""}',
            },
        )
    assert resp.status_code == 401
    assert resp.json()["detail"]["detail"] == "sts_verification_failed"


async def test_identity_exchange_empty_signed_request_returns_401() -> None:
    """Short-circuit before verifier when `signed_request` is empty."""
    async with _client() as c:
        resp = await c.post(
            "/api/v1/identity/exchange",
            json={"agent_pod_id": str(uuid4()), "version": "0.0.1", "signed_request": ""},
        )
    assert resp.status_code == 401
    assert "empty" in resp.json()["detail"]["detail"]
