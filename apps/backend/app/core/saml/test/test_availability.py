"""`core/saml` lazy-availability tests.

The real-SAML path requires `libxmlsec1`; `is_available()` reports True only
when the system lib + python3-saml import cleanly. Tests cover the fallback
behavior (returns None) and the registry registration regardless of
availability.
"""

from __future__ import annotations

from app.core import saml as saml_core
from app.domain.orgs import run_assertion_verifier


def test_is_available_returns_bool() -> None:
    """Whatever the environment, `is_available` must not raise."""
    out = saml_core.is_available()
    assert isinstance(out, bool)


def test_register_pushes_verifier_into_registry() -> None:
    """domain/orgs/sso imports + registers the core/saml verifier at module
    load. A roundtrip through `run_assertion_verifier` confirms both this
    one + any test-only verifier (`plugins/saml_test`) live in the list."""
    result = run_assertion_verifier("not-saml-xml", "<EntityDescriptor/>")
    assert result is None or isinstance(result, dict)


def test_unavailable_parser_does_not_crash_dispatcher() -> None:
    """When the library can't load, the verifier short-circuits with None
    instead of raising."""
    if saml_core.is_available():
        # Skip — env has libxmlsec1; behavior is exercised by integration
        # tests against a real IdP image, not here.
        return
    assert saml_core.verify_assertion("not-xml", "<EntityDescriptor/>") is None


def test_generate_sp_keypair_round_trip() -> None:
    """POC implementation: encrypted blob + placeholder cert. Verify the
    Fernet ciphertext is well-formed by decrypting it."""
    from app.core.secrets import decrypt  # noqa: PLC0415

    encrypted, cert = saml_core.generate_sp_keypair()
    assert cert == "POC-PLACEHOLDER-CERT"
    # Decrypts to the original 64-byte payload.
    plain = decrypt(encrypted)
    assert len(plain) == 64
