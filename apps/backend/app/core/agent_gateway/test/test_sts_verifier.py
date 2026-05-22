"""STS replay verifier — Vault AWS auth pattern.

Verifies the `core/agent_gateway/sts_verifier.py` module:
- `parse_signed_request` rejects bad JSON, missing fields, non-STS
  endpoints, and wrong body.
- `replay_caller_identity` posts the supplied headers + body to the
  STS endpoint via a caller-supplied `httpx.AsyncClient`, parses the
  ARN out of the response, and rejects non-2xx responses /
  missing-Arn responses.

Tests use `httpx.MockTransport` so no actual AWS call is made.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.agent_gateway.sts_verifier import (
    InvalidSignedRequestError,
    parse_signed_request,
    replay_caller_identity,
)

# ── parse_signed_request ──────────────────────────────────────────────────


def _good_signed_request_dict() -> dict:
    return {
        "url": "https://sts.amazonaws.com/",
        "headers": {
            "Authorization": (
                "AWS4-HMAC-SHA256 Credential=AKIATEST/20260522/us-east-1/sts/aws4_request, "
                "SignedHeaders=host;x-amz-date, Signature=deadbeef"
            ),
            "X-Amz-Date": "20260522T120000Z",
            "Host": "sts.amazonaws.com",
        },
        "body": "Action=GetCallerIdentity&Version=2011-06-15",
    }


def test_parse_signed_request_happy_path() -> None:
    raw = json.dumps(_good_signed_request_dict())
    parsed = parse_signed_request(raw)
    assert parsed.url == "https://sts.amazonaws.com/"
    assert parsed.body == "Action=GetCallerIdentity&Version=2011-06-15"
    # Headers normalized to lowercase keys (AWS sigv4 is case-insensitive).
    assert "authorization" in parsed.headers


def test_parse_signed_request_regional_endpoint_ok() -> None:
    d = _good_signed_request_dict()
    d["url"] = "https://sts.us-west-2.amazonaws.com/"
    parsed = parse_signed_request(json.dumps(d))
    assert "sts.us-west-2" in parsed.url


def test_parse_signed_request_rejects_non_sts_host() -> None:
    d = _good_signed_request_dict()
    d["url"] = "https://attacker.example.com/"
    with pytest.raises(InvalidSignedRequestError, match="not an STS endpoint"):
        parse_signed_request(json.dumps(d))


def test_parse_signed_request_rejects_sts_lookalike() -> None:
    """`sts.amazonaws.com.attacker.example` matches `*.amazonaws.com.*`
    but NOT our anchored host regex. Important: the regex must reject
    suffix-attack URLs."""
    d = _good_signed_request_dict()
    d["url"] = "https://sts.amazonaws.com.attacker.example/"
    with pytest.raises(InvalidSignedRequestError, match="not an STS endpoint"):
        parse_signed_request(json.dumps(d))


def test_parse_signed_request_rejects_wrong_body() -> None:
    d = _good_signed_request_dict()
    d["body"] = "Action=AssumeRole&Version=2011-06-15"
    with pytest.raises(InvalidSignedRequestError, match="body must be exactly"):
        parse_signed_request(json.dumps(d))


def test_parse_signed_request_rejects_missing_authorization() -> None:
    d = _good_signed_request_dict()
    d["headers"] = {"X-Amz-Date": "20260522T120000Z", "Host": "sts.amazonaws.com"}
    with pytest.raises(InvalidSignedRequestError, match="missing Authorization"):
        parse_signed_request(json.dumps(d))


def test_parse_signed_request_rejects_bad_json() -> None:
    with pytest.raises(InvalidSignedRequestError, match="not valid JSON"):
        parse_signed_request("not-a-json-object")


def test_parse_signed_request_rejects_non_object_json() -> None:
    with pytest.raises(InvalidSignedRequestError, match="must be a JSON object"):
        parse_signed_request('["array", "not", "object"]')


# ── replay_caller_identity ────────────────────────────────────────────────


_GOOD_STS_RESPONSE = b"""\
<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
  <GetCallerIdentityResult>
    <UserId>AROAEXAMPLEID</UserId>
    <Account>123456789012</Account>
    <Arn>arn:aws:sts::123456789012:assumed-role/yaaos-agent/task-abc</Arn>
  </GetCallerIdentityResult>
  <ResponseMetadata><RequestId>r-1</RequestId></ResponseMetadata>
</GetCallerIdentityResponse>
"""


@pytest.mark.asyncio
async def test_replay_returns_caller_arn() -> None:
    """Mock AWS STS to return a valid GetCallerIdentity response; the
    verifier returns the ARN as a plain string."""

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert "sts.amazonaws.com" in request.url.host
        assert request.headers.get("authorization", "").startswith("AWS4-HMAC-SHA256")
        return httpx.Response(200, content=_GOOD_STS_RESPONSE)

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        signed = parse_signed_request(json.dumps(_good_signed_request_dict()))
        arn = await replay_caller_identity(signed, client=client)
    assert arn == "arn:aws:sts::123456789012:assumed-role/yaaos-agent/task-abc"


@pytest.mark.asyncio
async def test_replay_rejects_non_200() -> None:
    """AWS returns 403 + an error XML for an invalid signature. The
    verifier must NOT leak the body, just raise InvalidSignedRequestError
    with the status code."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            content=b"<Error><Code>InvalidClientTokenId</Code></Error>",
        )

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        signed = parse_signed_request(json.dumps(_good_signed_request_dict()))
        with pytest.raises(InvalidSignedRequestError, match="HTTP 403"):
            await replay_caller_identity(signed, client=client)


@pytest.mark.asyncio
async def test_replay_rejects_response_without_arn_tag() -> None:
    """A 200 response that doesn't include <Arn>...</Arn> is rejected
    — AWS would never return that, and an MITM forging a 200 won't be
    able to either."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<GetCallerIdentityResponse/>")

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        signed = parse_signed_request(json.dumps(_good_signed_request_dict()))
        with pytest.raises(InvalidSignedRequestError, match="missing <Arn>"):
            await replay_caller_identity(signed, client=client)


@pytest.mark.asyncio
async def test_replay_rejects_non_aws_arn_format() -> None:
    """If the response's `<Arn>` value doesn't start with `arn:aws:`,
    reject — likely the response is forged or malformed."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<GetCallerIdentityResponse><GetCallerIdentityResult>"
            b"<Arn>not-an-arn</Arn></GetCallerIdentityResult>"
            b"</GetCallerIdentityResponse>",
        )

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        signed = parse_signed_request(json.dumps(_good_signed_request_dict()))
        with pytest.raises(InvalidSignedRequestError, match="unrecognized ARN format"):
            await replay_caller_identity(signed, client=client)


@pytest.mark.asyncio
async def test_replay_rejects_http_error() -> None:
    """Transport-level errors (timeout, connection refused) become
    InvalidSignedRequestError too — don't surface raw httpx exceptions
    to the endpoint."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated")

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        signed = parse_signed_request(json.dumps(_good_signed_request_dict()))
        with pytest.raises(InvalidSignedRequestError, match="HTTP error"):
            await replay_caller_identity(signed, client=client)
