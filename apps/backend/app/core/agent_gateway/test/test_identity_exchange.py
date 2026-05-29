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

from app.core.agent_gateway.models import BearerTokenRow, WorkspaceAgentRow
from app.core.agent_gateway.sts_verifier import (
    FailureCategory,
    InvalidSignedRequestError,
    VerifiedIdentity,
    reset_nonce_cache_for_tests,
    set_verify_identity_override,
)
from app.core.tenancy import update_org_fields
from app.domain.orgs import repository as orgs_repo


def _verified(canonical_arn: str, region: str = "us-east-1", raw_arn: str | None = None) -> VerifiedIdentity:
    return VerifiedIdentity(
        canonical_arn=canonical_arn,
        raw_arn=raw_arn or canonical_arn,
        region=region,
    )


def _app() -> FastAPI:

    app = FastAPI()
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"agent_gateway"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest.fixture(autouse=True)
def _reset_verifier():
    reset_nonce_cache_for_tests()
    yield
    set_verify_identity_override(None)
    reset_nonce_cache_for_tests()


async def test_identity_exchange_happy_path_persists_agent_row(db_session) -> None:
    """Valid signed_request → verifier returns canonical ARN → ARN matches
    a registered org → region matches → workspace_agents row inserted +
    real (hashed) bearer returned + bearer ledger row written."""
    canonical_arn = "arn:aws:iam::123456789012:role/yaaos-agent"
    raw_arn = "arn:aws:sts::123456789012:assumed-role/yaaos-agent/task-abc"
    org = await orgs_repo.insert_org(db_session, slug=f"sts-{uuid4().hex[:6]}")
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
    # Real bearer: ~43-char urlsafe-base64 secret, not a placeholder string.
    bearer = body["bearer"]
    assert not bearer.startswith("placeholder-")
    assert len(bearer) >= 40
    assert body["agent_id"]

    rows = (
        (await db_session.execute(select(WorkspaceAgentRow).where(WorkspaceAgentRow.org_id == org.org_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].iam_arn == canonical_arn
    assert rows[0].agent_pod_id == pod_id

    # Bearer ledger row exists, hashed (no plaintext).
    bearer_rows = (
        (await db_session.execute(select(BearerTokenRow).where(BearerTokenRow.org_id == org.org_id)))
        .scalars()
        .all()
    )
    assert len(bearer_rows) == 1
    assert bearer_rows[0].token_hash != bearer.encode("utf-8")
    assert bearer_rows[0].revoked_at is None


async def test_identity_exchange_unregistered_arn_returns_403(db_session) -> None:
    """Verifier returns an ARN; no org has that ARN registered → 403."""
    canonical_arn = "arn:aws:iam::999999999999:role/unregistered"

    async def _stub(_payload: str) -> VerifiedIdentity:
        return _verified(canonical_arn)

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
    rows = (await db_session.execute(select(WorkspaceAgentRow))).scalars().all()
    assert rows == []
    bearer_rows = (await db_session.execute(select(BearerTokenRow))).scalars().all()
    assert bearer_rows == []


async def test_identity_exchange_region_mismatch_returns_401(db_session) -> None:
    """Verified ARN matches an org, but the signed URL targets a different
    region than the org's pinned `aws_region` → 401 region_mismatch."""
    canonical_arn = "arn:aws:iam::123456789012:role/yaaos-agent"
    org = await orgs_repo.insert_org(db_session, slug=f"sts-{uuid4().hex[:6]}")
    await update_org_fields(
        db_session,
        org.org_id,
        registered_iam_arn=canonical_arn,
        aws_region="us-east-1",
    )
    await db_session.commit()

    async def _stub(_payload: str) -> VerifiedIdentity:
        return _verified(canonical_arn, region="eu-west-1")

    set_verify_identity_override(_stub)

    async with _client() as c:
        resp = await c.post(
            "/api/v1/identity/exchange",
            json={
                "agent_pod_id": str(uuid4()),
                "version": "0.0.1",
                "signed_request": '{"url":"https://sts.eu-west-1.amazonaws.com/","headers":{},"body":""}',
            },
        )
    assert resp.status_code == 401
    assert resp.json()["detail"]["detail"] == "sts_verification_failed"
    # No agent row or bearer issued.
    rows = (await db_session.execute(select(WorkspaceAgentRow))).scalars().all()
    assert rows == []
    bearer_rows = (await db_session.execute(select(BearerTokenRow))).scalars().all()
    assert bearer_rows == []


async def test_identity_exchange_invalid_signature_returns_401(db_session) -> None:
    """Verifier raises InvalidSignedRequestError → 401."""
    del db_session

    async def _stub(_payload: str) -> VerifiedIdentity:
        raise InvalidSignedRequestError("forged signature", FailureCategory.AWS_REJECTED)

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
