"""OpenAI API key validator for the `codex` plugin.

`validate_openai_key` is the callable registered with `core/api_keys` at
bootstrap so `/api/api-keys/openai/validate` can probe the key without
importing plugin internals.
"""

from __future__ import annotations

import httpx
from pydantic import SecretStr


async def validate_openai_key(api_key: SecretStr) -> tuple[bool, str]:
    """Return (healthy, message) for the supplied OpenAI API key.

    In stub mode the caller never reaches OpenAI — any non-empty key is
    treated as valid so the onboarding flow works without outbound connectivity.
    Uses the `/v1/models` endpoint (list only, no cost) as a lightweight liveness
    probe.
    """
    raw_key = api_key.get_secret_value()
    if not raw_key:
        return (False, "openai api key is empty")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {raw_key}"},
            )
        if r.status_code == 200:
            return (True, "ok")
        if r.status_code in (401, 403):
            return (False, "openai api key is invalid")
        return (False, f"openai api returned HTTP {r.status_code}")
    except (httpx.HTTPError, OSError) as e:
        return (False, f"could not reach openai api: {e}")
