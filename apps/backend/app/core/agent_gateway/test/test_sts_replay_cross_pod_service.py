"""STS replay rejection across independent default cache-provider contexts.

The real shared provider protects the nonce; only outbound AWS HTTP is stubbed.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio

from app.core.agent_gateway import sts_verifier
from app.core.agent_gateway.sts_verifier import (
    FailureCategory,
    InvalidSignedRequestError,
    VerifiedIdentity,
    verify_identity,
)
from app.core.cache import CacheError, set_cache_for_tests, shutdown

pytestmark = pytest.mark.usefixtures("redis_or_skip")

# ── STS mock ──────────────────────────────────────────────────────────────

_GOOD_STS_RESPONSE = b"""\
<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
  <GetCallerIdentityResult>
    <UserId>AROAEXAMPLEID</UserId>
    <Account>123456789012</Account>
    <Arn>arn:aws:sts::123456789012:assumed-role/yaaos-agent/task-replay-test</Arn>
  </GetCallerIdentityResult>
  <ResponseMetadata><RequestId>r-replay</RequestId></ResponseMetadata>
</GetCallerIdentityResponse>
"""


def _sts_mock_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=_GOOD_STS_RESPONSE)


def _unique_signed_request() -> str:
    """Build a well-formed signed STS request JSON string with a unique
    per-call Signature to ensure the nonce key is fresh each test run.
    """
    unique_sig = secrets.token_hex(32)
    return json.dumps(
        {
            "url": "https://sts.amazonaws.com/",
            "headers": {
                "Authorization": (
                    f"AWS4-HMAC-SHA256 Credential=AKIATEST/20260615/us-east-1/sts/aws4_request, "
                    f"SignedHeaders=host;x-amz-date, Signature={unique_sig}"
                ),
                "X-Amz-Date": "20260615T120000Z",
                "Host": "sts.amazonaws.com",
            },
            "body": "Action=GetCallerIdentity&Version=2011-06-15",
        }
    )


# ── Fixture: inject mock STS client, restore on exit ─────────────────────


@pytest_asyncio.fixture
async def _mock_sts_replay() -> AsyncIterator[None]:
    """Replace the module-level STS replay client with a mock-transport
    client for the duration of this test, then restore the original."""
    original = sts_verifier._replay_client
    transport = httpx.MockTransport(_sts_mock_handler)
    sts_verifier._replay_client = httpx.AsyncClient(transport=transport)
    try:
        yield
    finally:
        await sts_verifier._replay_client.aclose()
        sts_verifier._replay_client = original


# ── Service test ──────────────────────────────────────────────────────────


@pytest.mark.service
@pytest.mark.asyncio
@pytest.mark.usefixtures("_mock_sts_replay")
async def test_cross_pod_replay_is_rejected() -> None:
    """Closing one provider context cannot clear a nonce seen by another pod."""
    envelope = _unique_signed_request()

    with set_cache_for_tests() as first_provider:
        try:
            result = await verify_identity(envelope)
            assert isinstance(result, VerifiedIdentity)
            assert result.canonical_arn == "arn:aws:iam::123456789012:role/yaaos-agent"
            assert result.region == "us-east-1"
        finally:
            await shutdown()

    with set_cache_for_tests() as second_provider:
        assert second_provider is not first_provider
        try:
            with pytest.raises(InvalidSignedRequestError) as exc_info:
                await verify_identity(envelope)
            assert exc_info.value.category == FailureCategory.REPLAY_DETECTED
        finally:
            await shutdown()


@pytest.mark.service
@pytest.mark.asyncio
@pytest.mark.usefixtures("_mock_sts_replay")
async def test_nonce_storage_failure_does_not_authenticate() -> None:
    async def unavailable(key: str, *, ttl_seconds: int) -> bool:
        raise CacheError("unavailable")

    with set_cache_for_tests(scenario="memory") as provider:
        provider.claim_once = unavailable
        with pytest.raises(CacheError):
            await verify_identity(_unique_signed_request())
