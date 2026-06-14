"""SAML SP primitives: SP keypair, assertion verification, metadata.

Moved here from `domain/orgs/sso` (keypair) and `plugins/saml`
(verifier) so the SAML mechanics live in one home that's free of domain
concerns. `domain/orgs/sso` imports from here at module load and registers
the verifier into its assertion-verifier list.

The verifier itself is a thin wrapper around `python3-saml`, which binds to
`libxmlsec1` at C-extension load time. Local dev / wheel-only environments
may not have the native library; `is_available()` reports whether the
import succeeded and `verify_assertion()` returns None when it didn't —
production deployments install libxmlsec1 + xmlsec1 (the docker image
ships them).
"""

from __future__ import annotations

import secrets

import structlog
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from app.core.secrets import encrypt

log = structlog.get_logger("core.saml")


class SamlNotAvailableError(RuntimeError):
    """`python3-saml` failed to import. Production deployments must have
    libxmlsec1 + xmlsec1 installed."""


def is_available() -> bool:
    """True when `python3-saml` imports cleanly."""
    try:
        import onelogin.saml2  # noqa: F401, PLC0415
    except Exception as exc:  # ImportError, OSError on missing libxmlsec1, etc.
        log.debug("core.saml.unavailable", error=str(exc))
        return False
    return True


def generate_sp_keypair() -> tuple[bytes, str]:
    """Mint an SP keypair for an org. POC: a random secret encrypted via
    `core/secrets`; production deployments swap this for real RSA via
    `cryptography.hazmat`. The schema + Fernet round-trip are exercised
    end-to-end, so the upgrade is mechanical."""
    raw = secrets.token_bytes(64)
    return encrypt(raw), "POC-PLACEHOLDER-CERT"


def parse_assertion(xml: str, settings_dict: dict) -> dict:
    """Verify + parse a SAML response XML against the per-org settings dict
    (built from `sso_configs.idp_metadata_xml` + SP private key). Returns
    `{"email", "name_id", "attributes"}` on success.

    Raises `SamlNotAvailableError` when the library isn't importable."""
    if not is_available():
        raise SamlNotAvailableError("python3-saml + libxmlsec1 not available")
    from onelogin.saml2.auth import OneLogin_Saml2_Auth  # noqa: PLC0415

    request_data = {
        "https": "on",
        "http_host": settings_dict.get("sp", {}).get("entityId", ""),
        "script_name": "/api/sso",
        "get_data": {},
        "post_data": {"SAMLResponse": xml},
    }
    auth = OneLogin_Saml2_Auth(request_data, settings_dict)
    auth.process_response()
    if auth.get_errors():
        raise SamlNotAvailableError(f"saml parse errors: {auth.get_errors()}")
    return {
        "email": auth.get_nameid(),
        "name_id": auth.get_nameid(),
        "attributes": auth.get_attributes(),
    }


def verify_assertion(saml_response: str, idp_metadata_xml: str) -> dict | None:
    """The callable `domain/orgs/sso` registers as an assertion verifier.
    Returns the parsed payload on success, None when the library can't load
    OR the parse fails. Errors logged at exception level for ops."""
    if not is_available():
        return None
    try:
        return parse_assertion(saml_response, {"idp_metadata_xml": idp_metadata_xml})
    except Exception as exc:
        # inside-span failure: SSO callback FastAPI span is active
        span = trace.get_current_span()
        span.record_exception(exc)
        span.set_status(StatusCode.ERROR, str(exc))
        log.exception("core.saml.parse_failed")
        return None


__all__ = [
    "SamlNotAvailableError",
    "generate_sp_keypair",
    "is_available",
    "parse_assertion",
    "verify_assertion",
]
