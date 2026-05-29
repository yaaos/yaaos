"""Registry for the org-by-ARN lookup seam.

`core/agent_gateway` cannot import `domain/orgs` (core < domain in the layer
order). Instead, `domain/orgs` registers a lookup function here at boot so
the identity-exchange handler can resolve an IAM ARN to an org without a
cross-layer import.

Usage:
  # domain/orgs registers at import time:
  register_org_arn_lookup(my_lookup_fn)

  # core/agent_gateway/web.py calls it:
  ref = await lookup_org_by_arn(canonical_arn)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class OrgArnRef:
    """Minimal org identity resolved from a registered IAM ARN.

    Returned by the registered lookup function so `core/agent_gateway`
    never needs to import `domain/orgs` or its models.
    """

    id: UUID
    aws_region: str | None


# Single-slot registry — only one lookup implementation is expected.
OrgArnLookupFn = Callable[[str], Awaitable[OrgArnRef | None]]

_LOOKUP: OrgArnLookupFn | None = None


def register_org_arn_lookup(fn: OrgArnLookupFn) -> None:
    """Register the lookup function. Called by `domain/orgs` at boot.

    Idempotent for the same callable; raises `RuntimeError` on a conflicting
    re-registration so a double-wiring bug surfaces at boot instead of
    silently swapping the singleton. Tests reset via
    `_reset_org_arn_lookup_for_tests` first.
    """
    global _LOOKUP
    if _LOOKUP is not None and _LOOKUP is not fn:
        raise RuntimeError("org_arn_lookup already registered — reset it before re-registering")
    _LOOKUP = fn


def _reset_org_arn_lookup_for_tests() -> None:
    """Clear the registry slot. Test-only escape hatch; imported directly from
    this submodule (kept out of the package `__all__`)."""
    global _LOOKUP
    _LOOKUP = None


async def lookup_org_by_arn(canonical_arn: str) -> OrgArnRef | None:
    """Resolve *canonical_arn* to an `OrgArnRef` via the registered lookup.

    Returns ``None`` when no org has that ARN registered, or when no lookup
    function is registered (e.g. in unit tests that don't need the full
    composition root).
    """
    if _LOOKUP is None:
        return None
    return await _LOOKUP(canonical_arn)
