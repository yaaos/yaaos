"""Per-org SSO config + middleware-side enforcement.

Owns the `sso_configs` table on the write side and the `sso_satisfied_for_org_id`
session-row column on the read side. The SP private key is per-org and encrypted
via `core/secrets` (same master key as TOTP).
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.secrets import encrypt
from app.domain.orgs.models import SsoConfigRow

log = structlog.get_logger("orgs.sso")


class SsoConfigError(ValueError):
    """Invalid metadata, missing required fields, etc."""


class ExemptOwnerWithoutTotpError(SsoConfigError):
    """Tried to set an exempt-Owner who hasn't enrolled + verified TOTP."""


def _generate_sp_keypair() -> tuple[bytes, str]:
    """Mint a placeholder SP keypair for the org. Production deployments
    swap this for real RSA via `cryptography.hazmat`; the POC uses a
    random secret so the schema and Fernet round-trip are exercised."""
    raw = secrets.token_bytes(64)
    return encrypt(raw), "POC-PLACEHOLDER-CERT"


async def upsert_config(
    session: AsyncSession,
    *,
    org_id: UUID,
    idp_metadata_xml: str,
    jit_enabled: bool = False,
    enabled: bool = False,
    exempt_owner_user_id: UUID | None = None,
) -> SsoConfigRow:
    """Insert or update the per-org SSO config. Caller must have checked
    `can_be_sso_exempt_owner` for `exempt_owner_user_id` if non-None."""
    if not idp_metadata_xml or "<" not in idp_metadata_xml:
        raise SsoConfigError("idp_metadata_xml must be non-empty XML")

    from sqlalchemy import select  # noqa: PLC0415

    existing = (
        await session.execute(select(SsoConfigRow).where(SsoConfigRow.org_id == org_id))
    ).scalar_one_or_none()
    if existing is None:
        encrypted_key, cert = _generate_sp_keypair()
        row = SsoConfigRow(
            org_id=org_id,
            idp_metadata_xml=idp_metadata_xml,
            jit_enabled=jit_enabled,
            enabled=enabled,
            exempt_owner_user_id=exempt_owner_user_id,
            sp_private_key_encrypted=encrypted_key,
            sp_certificate=cert,
        )
        session.add(row)
        await session.flush()
        return row
    existing.idp_metadata_xml = idp_metadata_xml
    existing.jit_enabled = jit_enabled
    existing.enabled = enabled
    existing.exempt_owner_user_id = exempt_owner_user_id
    existing.updated_at = datetime.now(UTC)
    await session.flush()
    return existing


async def get_config(session: AsyncSession, *, org_id: UUID) -> SsoConfigRow | None:
    from sqlalchemy import select  # noqa: PLC0415

    return (
        await session.execute(select(SsoConfigRow).where(SsoConfigRow.org_id == org_id))
    ).scalar_one_or_none()


def sp_metadata_xml(org_slug: str, base_url: str) -> str:
    """Return the SP metadata XML the operator hands to their IdP. The POC
    emits a minimal envelope; production deployments swap this for the
    python3-saml-generated descriptor."""
    acs_url = f"{base_url}/api/sso/{org_slug}/acs"
    entity_id = f"{base_url}/sso/{org_slug}"
    return (
        '<?xml version="1.0"?>\n'
        f'<EntityDescriptor entityID="{entity_id}" xmlns="urn:oasis:names:tc:SAML:2.0:metadata">'
        '<SPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
        f'<AssertionConsumerService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
        f'Location="{acs_url}" index="0"/>'
        "</SPSSODescriptor></EntityDescriptor>"
    )


__all__ = [
    "ExemptOwnerWithoutTotpError",
    "SsoConfigError",
    "get_config",
    "register_assertion_verifier",
    "run_assertion_verifier",
    "sp_metadata_xml",
    "upsert_config",
]


# ── Assertion-verifier registry ──────────────────────────────────────────
#
# Plugins (`plugins/saml`, `plugins/saml_test`) register a callable that
# takes the raw `SAMLResponse` field + the per-org `idp_metadata_xml` and
# returns either a parsed payload dict (`{"email", "name_id", ...}`) or
# `None` on rejection. Inverts the layering — orgs/sso doesn't import the
# plugins; the plugins push themselves into this registry at import time.

from collections.abc import Callable  # noqa: E402

_verifiers: list[Callable[[str, str], dict | None]] = []


def register_assertion_verifier(fn: Callable[[str, str], dict | None]) -> None:
    _verifiers.append(fn)


def run_assertion_verifier(saml_response: str, idp_metadata_xml: str) -> dict | None:
    """Try each registered verifier in order. First non-None wins."""
    for fn in _verifiers:
        try:
            out = fn(saml_response, idp_metadata_xml)
        except Exception:
            log.exception("orgs.sso.verifier_failed")
            continue
        if out is not None:
            return out
    return None
