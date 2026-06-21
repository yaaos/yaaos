"""Stub SAML signer/verifier sanity tests + dispatcher registration check."""

from __future__ import annotations

import pytest

from app.domain.orgs import run_assertion_verifier
from app.plugins.saml_test.service import sign_assertion, verify_assertion


def test_sign_and_verify_roundtrip() -> None:
    payload = {"email": "x@y.test", "name_id": "x"}
    token = sign_assertion(payload)
    assert isinstance(token, str)
    out = verify_assertion(token)
    assert out is not None and out["email"] == "x@y.test"


def test_verify_garbage_returns_none() -> None:
    assert verify_assertion("not-a-signed-token") is None


def test_dispatcher_registers_test_verifier() -> None:
    """`bootstrap()` runs at import; the registry should accept a test
    assertion via `run_assertion_verifier`."""
    token = sign_assertion({"email": "abc@example.test", "name_id": "abc"})
    out = run_assertion_verifier(token, "<EntityDescriptor/>")
    assert out is not None
    assert out["email"] == "abc@example.test"


def test_dispatcher_returns_none_for_garbage() -> None:
    assert run_assertion_verifier("bad-token", "<EntityDescriptor/>") is None


@pytest.mark.asyncio
async def test_signed_payload_carries_email_field() -> None:
    """End-to-end: the signed payload is recoverable as JSON via the
    serializer's loads call."""
    token = sign_assertion({"email": "e@example.test", "name_id": "e", "custom": 1})
    out = verify_assertion(token)
    assert out["custom"] == 1
