"""STS replay protection — cross-pod Redis gate.

Verifies that `verify_identity` rejects a replayed signed envelope on any
pod — the nonce lives in shared Redis, not in process-local memory.

Simulates two pods by running two `verify_identity` calls in the same
process, both hitting the same Redis instance.  The first call writes the
nonce and returns a `VerifiedIdentity`; the second call finds the key and
raises `InvalidSignedRequestError(REPLAY_DETECTED)`.

Requires live Redis (fixture `redis_or_skip`).  No mocks — real Redis
`SET NX EX` is the substrate under test.  The STS replay itself uses an
`httpx.MockTransport` so no actual AWS call is made.
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
    per-call Signature to ensure the Redis nonce key is fresh each test run.
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
    """Same signed envelope presented to two verify_identity calls (simulating
    two backend pods sharing Redis) — first call succeeds; second is rejected
    as a replay."""
    # Unique per test run so the Redis nonce key is always fresh.
    envelope = _unique_signed_request()

    # First "pod": nonce is absent in Redis → insert succeeds → identity returned.
    result = await verify_identity(envelope)
    assert isinstance(result, VerifiedIdentity)
    assert result.canonical_arn == "arn:aws:iam::123456789012:role/yaaos-agent"
    assert result.region == "us-east-1"

    # Second "pod": same envelope → nonce already in Redis → replay rejected.
    with pytest.raises(InvalidSignedRequestError) as exc_info:
        await verify_identity(envelope)

    assert exc_info.value.category == FailureCategory.REPLAY_DETECTED
