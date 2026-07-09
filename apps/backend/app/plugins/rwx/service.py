"""RWX API key validator for `core/api_keys`.

The validator runs at request time when the API Keys UI's "Test key" button is
pressed. Sends a GET to cloud.rwx.com/mint/api/orgs with the user-supplied token
as a Bearer credential — the HTTP equivalent of `rwx whoami`. Returns True on
2xx, False otherwise. No retries — this is a wired-the-key-right check.

`bootstrap()` registers the validator with `core/api_keys` so the generic
`/api/api-keys/rwx/validate` endpoint can dispatch to it. Worker does not import
this plugin (validators serve the web validate endpoint; key delivery is
validator-independent).
"""

from __future__ import annotations

import httpx
import structlog

log = structlog.get_logger("plugins.rwx.service")

_RWX_ORGS_URL = "https://cloud.rwx.com/mint/api/orgs"
_VALIDATE_TIMEOUT_SECONDS = 10.0


async def validate_rwx_token(key: str) -> bool:
    """Probe the RWX API with the supplied token. Returns True on 2xx, False otherwise.

    Catches all transport errors and logs at warning level — a network blip is
    reported as "key invalid" rather than 5xx'ing the validate endpoint, matching
    the UX of "Test failed; check the key and try again".
    """
    headers = {"Authorization": f"Bearer {key}"}
    try:
        async with httpx.AsyncClient(timeout=_VALIDATE_TIMEOUT_SECONDS) as http:
            resp = await http.get(_RWX_ORGS_URL, headers=headers)
    except httpx.HTTPError as exc:
        log.warning("rwx.validate.transport_error", error=str(exc))
        return False
    if 200 <= resp.status_code < 300:
        return True
    log.warning(
        "rwx.validate.non_2xx",
        status=resp.status_code,
        body=resp.text[:300],
    )
    return False


def bootstrap() -> None:
    """Register the RWX validator with `core/api_keys`.

    Called once from `__init__.py` at import time so `web.py`'s step-6 plugin
    import triggers registration. The in-function import keeps the
    `core/api_keys` edge out of the top-level namespace.
    """
    from app.core.api_keys import register_validator as _register_validator  # noqa: PLC0415

    _register_validator("rwx", validate_rwx_token)
