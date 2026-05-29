"""STS GetCallerIdentity replay verifier — the Vault AWS auth pattern.

The agent supplies a sigv4-signed POST to AWS STS GetCallerIdentity in
its `IdentityExchangeRequest.signed_request`. The control plane doesn't
trust the agent's claim about its own ARN — instead it *replays* the
exact signed request against AWS and reads the ARN out of AWS's
response. yaaos's trust depends on AWS's signature verification, not on
the network path or anything the agent itself says.

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
   (category `parse_error`).
2. Rejects requests whose URL doesn't match an allowlist of known STS
   hostnames (category `endpoint_disallowed`) — prevents an attacker
   from pointing the replay at their own ARN-spoofing endpoint.
3. Rejects bodies that aren't exactly the GetCallerIdentity payload
   (category `body_mismatch`).
4. Rejects request envelopes whose `(Authorization || X-Amz-Date)` has
   been seen recently (category `replay_detected`) — defence in depth
   on top of sigv4's own 5-min validity window.
5. Replays the exact request via an httpx client pinned to TLS 1.3.
6. Classifies AWS rejections: `RequestExpired` → `clock_skew`, others
   → `aws_rejected`.
7. Canonicalizes the returned ARN — assumed-role ARNs (the shape STS
   returns for EC2/EKS/Fargate workloads) get rewritten to the
   underlying IAM role ARN so org lookup matches the value the customer
   pasted in the Workspace settings page.
8. Returns the canonical ARN + raw ARN + URL region. The
   `/identity/exchange` endpoint then checks the URL region against
   `orgs.aws_region` and rejects mismatches with `region_mismatch`.
"""

from __future__ import annotations

import json
import re
import ssl
import time
from collections import OrderedDict
from dataclasses import dataclass
from enum import StrEnum

import httpx
import structlog

log = structlog.get_logger("core.agent_gateway.sts_verifier")


# AWS STS endpoint allowlist. Global endpoint + regional endpoints. The
# region check happens later (against `orgs.aws_region`); this just blocks
# entirely-unknown hosts so a parse error catches them before replay.
_STS_HOST_RE = re.compile(r"^sts(?:\.(?P<region>[a-z0-9-]+))?\.amazonaws\.com$")

# The only body the replay endpoint accepts. Agents that signed anything
# else would have been signing some other API call.
_REQUIRED_BODY = "Action=GetCallerIdentity&Version=2011-06-15"

# Region attribution for the global STS endpoint (`sts.amazonaws.com` lives
# in us-east-1 physically; AWS docs treat it as the us-east-1 endpoint).
_GLOBAL_STS_REGION = "us-east-1"

# Replay-protection window. sigv4's own validity is 5 min; we layer 10 min
# of nonce-tracking on top to refuse exact reuse even before sigv4 expires.
_NONCE_TTL_SECONDS = 10 * 60
_NONCE_MAX_ENTRIES = 10_000


class FailureCategory(StrEnum):
    """Categorization of `InvalidSignedRequestError` for audit rows + UI."""

    PARSE_ERROR = "parse_error"
    ENDPOINT_DISALLOWED = "endpoint_disallowed"
    BODY_MISMATCH = "body_mismatch"
    REPLAY_DETECTED = "replay_detected"
    AWS_REJECTED = "aws_rejected"
    CLOCK_SKEW = "clock_skew"


class InvalidSignedRequestError(Exception):
    """Raised when the signed STS request is malformed, points at a
    non-STS endpoint, replays an envelope we've already seen, or AWS
    rejects the signature on replay. `category` carries the failure
    class for audit-row event types + UI failure feed."""

    def __init__(self, message: str, category: FailureCategory) -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class SignedSTSRequest:
    """Parsed sigv4-signed STS replay request."""

    url: str
    headers: dict[str, str]
    body: str
    region: str  # Region extracted from the URL host.


@dataclass(frozen=True)
class VerifiedIdentity:
    """Result of a successful verify_identity call.

    `canonical_arn` is the IAM role ARN — what the customer pastes in the
    Workspace settings page. `raw_arn` is exactly what STS returned (may
    be an assumed-role ARN). `region` is the AWS region extracted from
    the signed request URL — caller must check this against the org's
    pinned `aws_region` and reject mismatches.
    """

    canonical_arn: str
    raw_arn: str
    region: str


# ── ARN canonicalization ────────────────────────────────────────────────


_ASSUMED_ROLE_ARN_RE = re.compile(r"^arn:aws:sts::(?P<account>\d{12}):assumed-role/(?P<role>[^/]+)/.+$")


def canonicalize_arn(arn: str) -> str:
    """Normalize an STS-returned ARN to the IAM role ARN form.

    Production agents run under assumed roles (EC2 instance profile, EKS
    IRSA, ECS task role) and STS returns
    `arn:aws:sts::ACCOUNT:assumed-role/ROLE/SESSION`. The customer
    registers `arn:aws:iam::ACCOUNT:role/ROLE` in the UI — so we strip
    the session and rewrite the prefix before org lookup.

    Output is always lowercased: IAM names are unique-case-insensitive in
    AWS (you cannot create `MyRole` and `myrole` in the same account), so
    case-insensitive matching is safe, and the registration endpoint
    lowercases on write to keep both sides aligned.

    IAM user / role ARNs pass through (still lowercased).
    """
    m = _ASSUMED_ROLE_ARN_RE.match(arn)
    if m:
        return f"arn:aws:iam::{m.group('account')}:role/{m.group('role')}".lower()
    return arn.lower()


# ── Replay-protection LRU ──────────────────────────────────────────────


class _NonceLRU:
    """In-process bounded TTL cache for replay protection.

    Keyed on a stable hash of the signed request envelope (Authorization
    header + X-Amz-Date). Single-process for POC — fly.io target is
    a single backend machine. Multi-replica deployments would lift this
    to Redis.
    """

    def __init__(self, ttl_seconds: int = _NONCE_TTL_SECONDS, max_entries: int = _NONCE_MAX_ENTRIES) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._store: OrderedDict[str, float] = OrderedDict()

    def _evict(self, now: float) -> None:
        # Drop expired front items.
        while self._store:
            _key, ts = next(iter(self._store.items()))
            if ts + self._ttl <= now:
                self._store.popitem(last=False)
            else:
                break
        # Bound size.
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    def check_and_add(self, key: str) -> bool:
        """Return True if the key is fresh (caller proceeds), False if it
        was already in the cache within the TTL window (replay)."""
        now = time.monotonic()
        self._evict(now)
        if key in self._store:
            return False
        self._store[key] = now
        return True

    def reset(self) -> None:
        self._store.clear()


_nonce_lru = _NonceLRU()


def reset_nonce_cache_for_tests() -> None:
    """Test hook: clear the replay-protection cache between tests."""
    _nonce_lru.reset()


def _nonce_key(headers: dict[str, str]) -> str:
    """Stable cache key from the request's signed envelope."""
    return f"{headers.get('authorization', '')}|{headers.get('x-amz-date', '')}"


# ── Parsing + replay ──────────────────────────────────────────────────


def parse_signed_request(raw: str) -> SignedSTSRequest:
    """Parse the agent's JSON-encoded signed-request payload."""
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidSignedRequestError(
            f"signed_request is not valid JSON: {exc}", FailureCategory.PARSE_ERROR
        ) from exc

    if not isinstance(decoded, dict):
        raise InvalidSignedRequestError("signed_request must be a JSON object", FailureCategory.PARSE_ERROR)
    url = decoded.get("url")
    headers = decoded.get("headers")
    body = decoded.get("body")
    if not isinstance(url, str) or not url:
        raise InvalidSignedRequestError("missing url", FailureCategory.PARSE_ERROR)
    if not isinstance(headers, dict) or not headers:
        raise InvalidSignedRequestError("missing headers", FailureCategory.PARSE_ERROR)
    if not isinstance(body, str):
        raise InvalidSignedRequestError("missing body", FailureCategory.PARSE_ERROR)

    try:
        parsed_host = httpx.URL(url).host
    except Exception as exc:
        raise InvalidSignedRequestError(f"unparseable url: {exc}", FailureCategory.PARSE_ERROR) from exc
    match = _STS_HOST_RE.match(parsed_host)
    if not match:
        raise InvalidSignedRequestError(
            f"url host {parsed_host!r} is not an STS endpoint",
            FailureCategory.ENDPOINT_DISALLOWED,
        )
    region = match.group("region") or _GLOBAL_STS_REGION

    if body != _REQUIRED_BODY:
        raise InvalidSignedRequestError(
            f"body must be exactly {_REQUIRED_BODY!r}; got {body!r}",
            FailureCategory.BODY_MISMATCH,
        )

    norm = {k.lower(): v for k, v in headers.items() if isinstance(k, str) and isinstance(v, str)}
    if "authorization" not in norm:
        raise InvalidSignedRequestError("missing Authorization header", FailureCategory.PARSE_ERROR)
    return SignedSTSRequest(url=url, headers=norm, body=body, region=region)


_ARN_TAG_RE = re.compile(r"<Arn>([^<]+)</Arn>")
# AWS returns `<Code>RequestExpired</Code>` on stale signatures and
# `<Code>InvalidClientTokenId</Code>` / `<Code>SignatureDoesNotMatch</Code>`
# on bad signatures. We only special-case the time-bounded one for the
# `clock_skew` category.
_AWS_CODE_RE = re.compile(r"<Code>([^<]+)</Code>")


def _build_tls13_ssl_context() -> ssl.SSLContext:
    """Build an SSL context that requires TLS 1.3.

    AWS STS supports TLS 1.3, fly.io's egress supports it, every modern
    Python TLS stack supports it. Pinning to 1.3 closes a downgrade-attack
    surface and signals intent.
    """
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    return ctx


_replay_client: httpx.AsyncClient | None = None


def _get_replay_client() -> httpx.AsyncClient:
    """Shared httpx client for the STS replay path.

    Single instance with connection pooling — replays happen on every
    `/identity/exchange`. TLS 1.3 pinned; 5s timeout caps the worst-case
    wait when AWS or the network is degraded.
    """
    global _replay_client
    if _replay_client is None:
        _replay_client = httpx.AsyncClient(
            timeout=httpx.Timeout(5.0),
            verify=_build_tls13_ssl_context(),
        )
    return _replay_client


async def replay_caller_identity(
    signed: SignedSTSRequest,
    *,
    client: httpx.AsyncClient,
) -> str:
    """Replay the signed request against AWS STS, return raw ARN."""
    try:
        response = await client.post(
            signed.url,
            headers=signed.headers,
            content=signed.body,
        )
    except httpx.HTTPError as exc:
        raise InvalidSignedRequestError(
            f"STS replay HTTP error: {exc}", FailureCategory.AWS_REJECTED
        ) from exc

    if response.status_code != 200:
        code_match = _AWS_CODE_RE.search(response.text)
        code = code_match.group(1) if code_match else None
        category = FailureCategory.CLOCK_SKEW if code == "RequestExpired" else FailureCategory.AWS_REJECTED
        log.warning(
            "sts_verifier.replay_rejected",
            status_code=response.status_code,
            aws_code=code,
        )
        raise InvalidSignedRequestError(
            f"STS replay rejected: HTTP {response.status_code} code={code}", category
        )

    match = _ARN_TAG_RE.search(response.text)
    if not match:
        raise InvalidSignedRequestError("STS response missing <Arn> tag", FailureCategory.AWS_REJECTED)
    arn = match.group(1).strip()
    if not arn.startswith("arn:aws:"):
        raise InvalidSignedRequestError(f"unrecognized ARN format: {arn!r}", FailureCategory.AWS_REJECTED)
    return arn


# ── Production entry point ────────────────────────────────────────────


_verify_override = None


async def verify_identity(signed_request: str) -> VerifiedIdentity:
    """Parse + replay-protect + replay + canonicalize. Returns
    `VerifiedIdentity` on success; raises `InvalidSignedRequestError`
    with a `FailureCategory` otherwise.

    Tests can swap the production path wholesale via
    `set_verify_identity_override(callback)`.
    """
    if _verify_override is not None:
        return await _verify_override(signed_request)
    signed = parse_signed_request(signed_request)
    if not _nonce_lru.check_and_add(_nonce_key(signed.headers)):
        raise InvalidSignedRequestError(
            "signed request envelope already seen within replay window",
            FailureCategory.REPLAY_DETECTED,
        )
    raw_arn = await replay_caller_identity(signed, client=_get_replay_client())
    return VerifiedIdentity(
        canonical_arn=canonicalize_arn(raw_arn),
        raw_arn=raw_arn,
        region=signed.region,
    )


def set_verify_identity_override(callback) -> None:  # type: ignore[no-untyped-def]
    """Test hook: swap the production `verify_identity` for a stub.

    Stubs return a `VerifiedIdentity`. Pass `None` to restore.
    """
    global _verify_override
    _verify_override = callback


__all__ = [
    "FailureCategory",
    "InvalidSignedRequestError",
    "SignedSTSRequest",
    "VerifiedIdentity",
    "canonicalize_arn",
    "parse_signed_request",
    "replay_caller_identity",
    "verify_identity",
]
