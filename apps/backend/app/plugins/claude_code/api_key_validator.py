"""Anthropic API key validator for `core/api_keys`.

The validator runs at request time when the API Keys UI's "Test key" button is
pressed. Sends a 1-token `messages.create` call to api.anthropic.com using
the user-supplied key; returns True on 2xx, False otherwise. No retries —
this is a wired-the-key-right check, not a health probe.
"""

from __future__ import annotations

import httpx
import structlog

log = structlog.get_logger("plugins.claude_code.api_key_validator")

_ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
_VALIDATE_TIMEOUT_SECONDS = 10.0


async def validate_anthropic_key(plaintext: str) -> bool:
    """Send a minimal request to Anthropic. Returns True on 2xx, False otherwise.

    Catches all transport errors and logs at warning level — a network blip
    is reported as "key invalid" rather than 5xx'ing the validate endpoint,
    matching the UX of "Test failed; check the key and try again".
    """
    headers = {
        "x-api-key": plaintext,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": "claude-haiku-4-5",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }
    try:
        async with httpx.AsyncClient(timeout=_VALIDATE_TIMEOUT_SECONDS) as http:
            resp = await http.post(_ANTHROPIC_API, headers=headers, json=body)
    except httpx.HTTPError as exc:
        log.warning("anthropic.validate.transport_error", error=str(exc))
        return False
    if 200 <= resp.status_code < 300:
        return True
    log.warning(
        "anthropic.validate.non_2xx",
        status=resp.status_code,
        body=resp.text[:300],
    )
    return False
