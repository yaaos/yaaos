"""STS GetCallerIdentity replay verifier — the Vault AWS auth pattern.

The agent supplies a sigv4-signed POST to AWS STS GetCallerIdentity in
its `IdentityExchangeRequest.signed_request`. The control plane doesn't
trust the agent's claim about its own ARN — instead it *replays* the
exact signed request against AWS and reads the ARN out of AWS's
response. This means yaaos's trust depends on AWS's signature
verification, not on the network path.

Wire format for `signed_request`: a JSON-encoded dict with these keys:
- `url`: the STS endpoint URL (`https://sts.amazonaws.com/` or a
  regional `sts.<region>.amazonaws.com` host).
- `headers`: dict of HTTP headers, must include `Authorization`,
  `X-Amz-Date`, and `Host`. The agent signs the request with sigv4
  using its task IAM role; the `Authorization` header carries the
  derived signature.
- `body`: the request body. Must be exactly
  `Action=GetCallerIdentity&Version=2011-06-15`.

The verifier:
1. Rejects requests where any field is missing / shape-malformed
   (`InvalidSignedRequestError`).
2. Rejects requests whose URL doesn't match an allowlist of known STS
   hostnames (`InvalidSignedRequestError`) — prevents an attacker from
   pointing the replay at their own ARN-spoofing endpoint.
3. Replays the exact request via `httpx.AsyncClient` (caller-supplied
   so tests can inject a `MockTransport`).
4. Parses `<Arn>...</Arn>` out of the `<GetCallerIdentityResult>` XML.
5. Returns the caller ARN, or raises `InvalidSignedRequestError` on a
   non-2xx response / missing Arn tag.

The caller (the `/identity/exchange` endpoint) then matches the ARN
against `orgs.registered_iam_arn` and rejects mismatches.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx
import structlog

log = structlog.get_logger("core.agent_gateway.sts_verifier")


# AWS STS endpoint allowlist. Global endpoint + the regional endpoints —
# we don't pin to one region because customers may run their workspace
# agent anywhere. Match the host portion only (path is always `/`).
_STS_HOST_RE = re.compile(r"^sts(?:\.[a-z0-9-]+)?\.amazonaws\.com$")

# The only body the replay endpoint accepts. Agents that signed
# anything else would have been signing some other API call.
_REQUIRED_BODY = "Action=GetCallerIdentity&Version=2011-06-15"


class InvalidSignedRequestError(Exception):
    """Raised when the signed STS request is malformed, points at a
    non-STS endpoint, or AWS rejects the signature on replay."""


@dataclass(frozen=True)
class SignedSTSRequest:
    """Parsed sigv4-signed STS replay request."""

    url: str
    headers: dict[str, str]
    body: str


def parse_signed_request(raw: str) -> SignedSTSRequest:
    """Parse the agent's JSON-encoded signed-request payload. Raises
    `InvalidSignedRequestError` on shape / format failures + endpoint
    allowlist mismatch."""
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidSignedRequestError(f"signed_request is not valid JSON: {exc}") from exc

    if not isinstance(decoded, dict):
        raise InvalidSignedRequestError("signed_request must be a JSON object")
    url = decoded.get("url")
    headers = decoded.get("headers")
    body = decoded.get("body")
    if not isinstance(url, str) or not url:
        raise InvalidSignedRequestError("missing url")
    if not isinstance(headers, dict) or not headers:
        raise InvalidSignedRequestError("missing headers")
    if not isinstance(body, str):
        raise InvalidSignedRequestError("missing body")

    # Validate the host is an STS endpoint. We don't trust the agent's
    # supplied URL otherwise; an attacker could otherwise point the
    # replay at a server they control.
    try:
        parsed_host = httpx.URL(url).host
    except Exception as exc:
        raise InvalidSignedRequestError(f"unparseable url: {exc}") from exc
    if not _STS_HOST_RE.match(parsed_host):
        raise InvalidSignedRequestError(f"url host {parsed_host!r} is not an STS endpoint")
    if body != _REQUIRED_BODY:
        raise InvalidSignedRequestError(f"body must be exactly {_REQUIRED_BODY!r}; got {body!r}")

    # Normalize header keys to lowercase for cross-platform consistency;
    # AWS sigv4 normalizes them at signing time so this doesn't affect
    # signature validity.
    norm = {k.lower(): v for k, v in headers.items() if isinstance(k, str) and isinstance(v, str)}
    if "authorization" not in norm:
        raise InvalidSignedRequestError("missing Authorization header")
    return SignedSTSRequest(url=url, headers=norm, body=body)


# AWS returns XML like `<GetCallerIdentityResult><Arn>arn:aws:…</Arn>…`.
# We accept a loose regex over the XML payload — namespace prefixes
# and whitespace are non-significant for our ARN extraction.
_ARN_TAG_RE = re.compile(r"<Arn>([^<]+)</Arn>")


async def replay_caller_identity(
    signed: SignedSTSRequest,
    *,
    client: httpx.AsyncClient,
) -> str:
    """Replay the signed request against AWS STS and return the caller's
    ARN. Raises `InvalidSignedRequestError` on a non-2xx response or
    when the response body doesn't include `<Arn>…</Arn>` (AWS's signal
    that the signature didn't validate). Caller supplies the
    `httpx.AsyncClient` so tests can pass a `MockTransport`."""
    try:
        response = await client.post(
            signed.url,
            headers=signed.headers,
            content=signed.body,
        )
    except httpx.HTTPError as exc:
        raise InvalidSignedRequestError(f"STS replay HTTP error: {exc}") from exc

    if response.status_code != 200:
        # AWS returns 403 with `<Error><Code>InvalidClientTokenId</Code>…`
        # for bad signatures. We log + raise without leaking the body
        # because it can contain headers the agent signed.
        log.warning(
            "sts_verifier.replay_rejected",
            status_code=response.status_code,
        )
        raise InvalidSignedRequestError(f"STS replay rejected: HTTP {response.status_code}")

    match = _ARN_TAG_RE.search(response.text)
    if not match:
        raise InvalidSignedRequestError("STS response missing <Arn> tag")
    arn = match.group(1).strip()
    if not arn.startswith("arn:aws:"):
        raise InvalidSignedRequestError(f"unrecognized ARN format: {arn!r}")
    return arn


# ── Production entry point ────────────────────────────────────────────────


_verify_override = None


async def verify_identity(signed_request: str) -> str:
    """Parse + replay the agent's signed STS request; return the caller
    ARN. Convenience wrapper for the `/identity/exchange` endpoint —
    parses, replays via a per-call `httpx.AsyncClient`, returns the ARN
    or raises `InvalidSignedRequestError`.

    Tests can swap the verifier wholesale via
    `set_verify_identity_override(stub)` so they don't need to thread a
    `MockTransport` through the endpoint plumbing.
    """
    if _verify_override is not None:
        return await _verify_override(signed_request)
    signed = parse_signed_request(signed_request)
    async with httpx.AsyncClient(timeout=15.0) as client:
        return await replay_caller_identity(signed, client=client)


def set_verify_identity_override(callback) -> None:  # type: ignore[no-untyped-def]
    """Test hook: swap the production `verify_identity` for a stub.
    Pass `None` to restore."""
    global _verify_override
    _verify_override = callback


__all__ = [
    "InvalidSignedRequestError",
    "SignedSTSRequest",
    "parse_signed_request",
    "replay_caller_identity",
    "set_verify_identity_override",
    "verify_identity",
]
