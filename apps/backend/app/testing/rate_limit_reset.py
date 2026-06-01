"""Cross-module test helper: flush identity-exchange rate-limit keys.

Lives here (not in `core/agent_gateway/rate_limit`) because cross-module test
machinery belongs in `app/testing/` — production code never resets a rate-limit
window. Consumes `rate_limit.KEY_PREFIX` so the Redis key shape stays owned by
the production module.
"""

from __future__ import annotations

from app.core.agent_gateway.rate_limit import KEY_PREFIX
from app.core.redis import delete_keys_matching


async def reset_rate_limit_for_tests() -> None:
    """Flush all identity-exchange rate-limit keys so consecutive tests using
    the same source IP don't bleed into each other's sliding windows."""
    try:
        await delete_keys_matching(f"{KEY_PREFIX}*")
    except Exception:
        pass
