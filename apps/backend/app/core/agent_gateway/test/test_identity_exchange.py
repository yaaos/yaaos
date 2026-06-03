"""`POST /api/v1/agent/identity` — verified ARN → org match → ensure_agent_row.

Service test against the assembled FastAPI app. The STS verifier itself
is unit-tested in `test_sts_verifier.py`; here we test the endpoint's
wiring of verifier + org-by-ARN lookup + agent-row persistence + 401/403
error mapping.

Tests use `set_verify_identity_override` to swap the production verifier
for a synchronous stub so no httpx machinery is needed.

Each test supplies a unique source IP via `_client(ip)` so per-IP rate-limit
windows never collide across tests. No Redis key resets are needed.
"""

from __future__ import annotations

import itertools
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

_ENDPOINT = "/api/v1/agent/identity"

# Per-test unique IP counter — each _unique_ip() call allocates one address
# from the 10.0.0.0/8 range so no test shares a rate-limit window with another.
_ip_counter = itertools.count(1)


def _unique_ip() -> str:
    n = next(_ip_counter)
    return f"10.{(n >> 16) & 0xFF}.{(n >> 8) & 0xFF}.{n & 0xFF}"


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


def _client(ip: str | None = None) -> httpx.AsyncClient:
    """Return an async client with a unique source IP.

    Each call allocates a fresh IP from the counter so the per-IP
    rate-limit window is guaranteed to be empty regardless of test order
    or parallel execution. Pass an explicit `ip` when a test needs two
    requests to share the same window (e.g. rotation tests).
    """
    host = ip or _unique_ip()
    transport = httpx.ASGITransport(app=_app(), client=(host, 12345))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture(autouse=True)
def _reset_verifier():
    reset_nonce_cache_for_tests()
    yield
    set_verify_identity_override(None)
    reset_nonce_cache_for_tests()


_AUDIENCE = "app.yaaos.cloud"
_SIGNED_PAYLOAD = (
    '{"url":"https://sts.amazonaws.com/","headers":{"x-yaaos-audience":"app.yaaos.cloud"},"body":""}'
)


async def test_identity_exchange_happy_path_persists_agent_row(db_session) -> None:
    """Valid payload → verifier returns canonical ARN with assumed-role raw ARN →
    ARN matches a registered org → region matches → workspace_agents row
    inserted keyed on instance_id + real (hashed) bearer returned + bearer
    ledger row written with issued_iam_arn."""
    canonical_arn = "arn:aws:iam::123456789012:role/yaaos-agent"
    raw_arn = "arn:aws:sts::123456789012:assumed-role/yaaos-agent/task-abc-123"
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

    async with _client() as c:
        resp = await c.post(
            _ENDPOINT,
            json={
                "kind": "aws-sts",
                "agent_version": "1.2.3",
                "agent_metadata": {"os": "linux", "cpu_count": 2, "memory_bytes": 8192},
                "payload": _SIGNED_PAYLOAD,
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Real bearer: ~43-char urlsafe-base64 secret.
    bearer = body["bearer"]
    assert not bearer.startswith("placeholder-")
    assert len(bearer) >= 40
    assert body["agent_id"]
    assert body["instance_id"] == "task-abc-123"
    assert "renewal_after" in body

    rows = (
        (await db_session.execute(select(WorkspaceAgentRow).where(WorkspaceAgentRow.org_id == org.org_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].iam_arn == canonical_arn
    assert rows[0].instance_id == "task-abc-123"
    assert rows[0].os == "linux"
    assert rows[0].cpu_count == 2
    assert rows[0].memory_bytes == 8192

    # Bearer ledger row: hashed, not plaintext; records issued_iam_arn.
    bearer_rows = (
        (await db_session.execute(select(BearerTokenRow).where(BearerTokenRow.org_id == org.org_id)))
        .scalars()
        .all()
    )
    assert len(bearer_rows) == 1
    assert bearer_rows[0].token_hash != bearer.encode("utf-8")
    assert bearer_rows[0].revoked_at is None
    assert bearer_rows[0].issued_iam_arn == canonical_arn


async def test_identity_exchange_bearer_ttl_is_one_hour(db_session) -> None:
    """Bearer `expires_at` is ~1 hour after issuance (not 24h)."""
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    canonical_arn = "arn:aws:iam::123456789012:role/yaaos-ttl-test"
    raw_arn = "arn:aws:sts::123456789012:assumed-role/yaaos-ttl-test/task-ttl"
    org = await orgs_repo.insert_org(db_session, slug=f"sts-ttl-{uuid4().hex[:6]}")
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

    before = datetime.now(UTC)
    async with _client() as c:
        resp = await c.post(
            _ENDPOINT,
            json={
                "kind": "aws-sts",
                "agent_version": "1.0.0",
                "payload": _SIGNED_PAYLOAD,
            },
        )
    after = datetime.now(UTC)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    expires_at_str = body["expires_at"]
    # Parse RFC3339/ISO8601
    if expires_at_str.endswith("Z"):
        expires_at_str = expires_at_str[:-1] + "+00:00"
    expires_at = datetime.fromisoformat(expires_at_str)

    # Should be roughly 1 hour from now.
    min_expected = before + timedelta(minutes=55)
    max_expected = after + timedelta(hours=1, minutes=5)
    assert min_expected <= expires_at <= max_expected, (
        f"expires_at {expires_at} is not within [55m, 65m] of issuance"
    )


async def test_identity_exchange_rotation_non_revoking(db_session) -> None:
    """Calling exchange twice issues a new bearer without revoking the old."""
    canonical_arn = "arn:aws:iam::123456789012:role/yaaos-rotate"
    raw_arn = "arn:aws:sts::123456789012:assumed-role/yaaos-rotate/task-rotate"
    org = await orgs_repo.insert_org(db_session, slug=f"sts-rot-{uuid4().hex[:6]}")
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

    payload = {
        "kind": "aws-sts",
        "agent_version": "1.0.0",
        "payload": _SIGNED_PAYLOAD,
    }

    async with _client() as c:
        first = await c.post(_ENDPOINT, json=payload)
        second = await c.post(_ENDPOINT, json=payload)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    first_bearer = first.json()["bearer"]
    second_bearer = second.json()["bearer"]
    assert first_bearer != second_bearer

    # Both bearers still active in the ledger.
    rows = (
        (await db_session.execute(select(BearerTokenRow).where(BearerTokenRow.org_id == org.org_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 2
    revoked = [r for r in rows if r.revoked_at is not None]
    assert len(revoked) == 0, "rotation must not revoke the old bearer"


async def test_identity_exchange_unregistered_arn_returns_403(db_session) -> None:
    """Verifier returns an ARN; no org has that ARN registered → 403."""
    canonical_arn = "arn:aws:iam::999999999999:role/unregistered"

    async def _stub(_payload: str) -> VerifiedIdentity:
        return _verified(canonical_arn)

    set_verify_identity_override(_stub)

    async with _client() as c:
        resp = await c.post(
            _ENDPOINT,
            json={
                "kind": "aws-sts",
                "agent_version": "0.0.1",
                "payload": _SIGNED_PAYLOAD,
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
            _ENDPOINT,
            json={
                "kind": "aws-sts",
                "agent_version": "0.0.1",
                "payload": _SIGNED_PAYLOAD,
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
            _ENDPOINT,
            json={
                "kind": "aws-sts",
                "agent_version": "0.0.1",
                "payload": _SIGNED_PAYLOAD,
            },
        )
    assert resp.status_code == 401
    assert resp.json()["detail"]["detail"] == "sts_verification_failed"


async def test_identity_exchange_empty_payload_returns_401() -> None:
    """Short-circuit before verifier when `payload` is empty."""
    async with _client() as c:
        resp = await c.post(
            _ENDPOINT,
            json={"kind": "aws-sts", "agent_version": "0.0.1", "payload": ""},
        )
    assert resp.status_code == 401
    assert "empty" in resp.json()["detail"]["detail"]


async def test_identity_exchange_unsupported_kind_returns_401() -> None:
    """Unsupported `kind` → 401 before reaching the verifier."""
    async with _client() as c:
        resp = await c.post(
            _ENDPOINT,
            json={"kind": "gcp-oidc", "agent_version": "0.0.1", "payload": "some-payload"},
        )
    assert resp.status_code == 401
    assert "unsupported kind" in resp.json()["detail"]["detail"]


@pytest.mark.service
async def test_identity_exchange_response_includes_org_id(db_session) -> None:
    """Successful exchange response carries org_id matching the org whose
    registered_iam_arn matched the verified canonical ARN."""
    canonical_arn = "arn:aws:iam::555555555555:role/yaaos-org-id-test"
    raw_arn = "arn:aws:sts::555555555555:assumed-role/yaaos-org-id-test/task-orgid"
    org = await orgs_repo.insert_org(db_session, slug=f"sts-orgid-{uuid4().hex[:6]}")
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
            _ENDPOINT,
            json={
                "kind": "aws-sts",
                "agent_version": "1.0.0",
                "payload": _SIGNED_PAYLOAD,
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "org_id" in body, "response must include org_id"
    assert body["org_id"] == str(org.org_id), (
        f"org_id in response ({body['org_id']}) must match the org whose ARN matched ({org.org_id})"
    )
    assert "instance_id" in body, "response must include instance_id"
    assert body["instance_id"] == "task-orgid"


async def test_identity_exchange_audience_mismatch_returns_401(db_session, monkeypatch) -> None:
    """Audience header in the payload does not match YAAOS_PUBLIC_HOSTNAME → 401."""
    del db_session

    import json as _json  # noqa: PLC0415

    from app.core.config import Settings  # noqa: PLC0415

    # Override the settings so YAAOS_PUBLIC_HOSTNAME is set to a known value.
    monkeypatch.setattr(
        "app.core.agent_gateway.web.get_settings",
        lambda: Settings.model_construct(yaaos_public_hostname="app.yaaos.cloud"),
    )

    async def _stub(_payload: str) -> VerifiedIdentity:  # unreachable after audience check
        return _verified("arn:aws:iam::123456789012:role/yaaos-agent")

    set_verify_identity_override(_stub)

    payload_with_wrong_audience = _json.dumps(
        {
            "url": "https://sts.amazonaws.com/",
            "headers": {
                "Authorization": "AWS4-HMAC-SHA256 ...",
                "X-Amz-Date": "20240101T000000Z",
                "Host": "sts.amazonaws.com",
                "x-yaaos-audience": "wrong.backend.example.com",
            },
            "body": "Action=GetCallerIdentity&Version=2011-06-15",
        }
    )

    async with _client() as c:
        resp = await c.post(
            _ENDPOINT,
            json={"kind": "aws-sts", "agent_version": "0.0.1", "payload": payload_with_wrong_audience},
        )
    assert resp.status_code == 401
    assert "audience_mismatch" in resp.json()["detail"]["detail"]


async def test_identity_exchange_missing_audience_returns_401(db_session, monkeypatch) -> None:
    """Empty/absent X-Yaaos-Audience when YAAOS_PUBLIC_HOSTNAME is set → 401."""
    del db_session

    import json as _json  # noqa: PLC0415

    from app.core.config import Settings  # noqa: PLC0415

    monkeypatch.setattr(
        "app.core.agent_gateway.web.get_settings",
        lambda: Settings.model_construct(yaaos_public_hostname="app.yaaos.cloud"),
    )

    async def _stub(_payload: str) -> VerifiedIdentity:  # unreachable after audience check
        return _verified("arn:aws:iam::123456789012:role/yaaos-agent")

    set_verify_identity_override(_stub)

    # Payload with no x-yaaos-audience header at all.
    payload_no_audience = _json.dumps(
        {
            "url": "https://sts.amazonaws.com/",
            "headers": {
                "Authorization": "AWS4-HMAC-SHA256 ...",
                "X-Amz-Date": "20240101T000000Z",
                "Host": "sts.amazonaws.com",
            },
            "body": "Action=GetCallerIdentity&Version=2011-06-15",
        }
    )

    async with _client() as c:
        resp = await c.post(
            _ENDPOINT,
            json={"kind": "aws-sts", "agent_version": "0.0.1", "payload": payload_no_audience},
        )
    assert resp.status_code == 401
    assert "audience_mismatch" in resp.json()["detail"]["detail"]
