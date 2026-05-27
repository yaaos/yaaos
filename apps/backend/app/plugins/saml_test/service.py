"""Test-only SAML IdP stub.

`sign_assertion(payload)` / `verify_assertion(token)` emit + verify
itsdangerous-signed tokens that stand in for SAML XML assertions. The
orchestration logic in `domain.orgs.sso` consumes the verified payload
as if it had come from a real `python3-saml` parse — the same fields
(`email`, `name_id`, `org_slug`) are present.
"""

from __future__ import annotations

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.config import get_settings

assert get_settings().yaaos_env == "test", "plugins.saml_test refuses to load outside YAAOS_ENV=test"


_SALT = "yaaos-saml-test-assertion"
_TTL_SECONDS = 600


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().yaaos_oauth_state_secret.get_secret_value(), salt=_SALT)


def sign_assertion(payload: dict) -> str:
    """Encode a stub SAML assertion. Tests call this to drive ACS."""
    return _serializer().dumps(payload)


def verify_assertion(token: str) -> dict | None:
    """Verify + return the payload. None on bad signature / expired."""
    try:
        return _serializer().loads(token, max_age=_TTL_SECONDS)
    except (BadSignature, SignatureExpired):
        return None


def _verify(saml_response: str, _idp_metadata_xml: str) -> dict | None:
    return verify_assertion(saml_response)


def bootstrap() -> None:
    """Register the test-stub verifier in `domain/orgs.sso`."""
    from app.domain.orgs import register_assertion_verifier  # noqa: PLC0415

    register_assertion_verifier(_verify)


__all__ = ["bootstrap", "sign_assertion", "verify_assertion"]
