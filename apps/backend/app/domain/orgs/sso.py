"""Per-org SSO config + middleware-side enforcement.

Owns the `sso_configs` table on the write side and the `sso_satisfied_for_org_id`
session-row column on the read side. The SP private key is per-org and encrypted
via `core/secrets` (same master key as TOTP).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.saml import generate_sp_keypair as _generate_sp_keypair
from app.core.saml import verify_assertion as _core_verify_assertion
from app.domain.orgs.models import SsoConfigRow
from app.domain.orgs.service import SsoConfig

log = structlog.get_logger("orgs.sso")


class SsoConfigError(ValueError):
    """Invalid metadata, missing required fields, etc."""


class ExemptOwnerWithoutTotpError(SsoConfigError):
    """Tried to set an exempt-Owner who hasn't enrolled + verified TOTP."""


def _normalize_email_domains(raw: list[str] | None) -> list[str]:
    """Lowercase + strip + dedupe + reject empty/`@`-prefixed entries.

    Domain claims are the routing key for `/api/sso/discover`; bad
    data here turns into login-page bugs. Reject anything containing a
    glob, `@`, or whitespace; allow ASCII-letter / digit / dot / dash.
    """
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for d in raw:
        cleaned = (d or "").strip().lower()
        if not cleaned:
            continue
        if any(ch in cleaned for ch in " \t\n@*"):
            raise SsoConfigError(f"invalid email domain: {d!r}")
        if cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


async def upsert_config(
    session: AsyncSession,
    *,
    org_id: UUID,
    idp_metadata_xml: str,
    jit_enabled: bool = False,
    enabled: bool = False,
    exempt_owner_user_id: UUID | None = None,
    email_domains: list[str] | None = None,
) -> SsoConfig:
    """Insert or update the per-org SSO config. Caller must have checked
    `can_be_sso_exempt_owner` for `exempt_owner_user_id` if non-None.

    Also keeps the denormalized `orgs.sso_enabled` and
    `orgs.sso_exempt_owner_user_id` columns in sync via
    `core/tenancy.set_sso_authz_for_org`.
    """
    if not idp_metadata_xml or "<" not in idp_metadata_xml:
        raise SsoConfigError("idp_metadata_xml must be non-empty XML")

    domains = _normalize_email_domains(email_domains)

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
            email_domains=domains,
        )
        session.add(row)
        await session.flush()
        cfg = SsoConfig.from_row(row)
    else:
        existing.idp_metadata_xml = idp_metadata_xml
        existing.jit_enabled = jit_enabled
        existing.enabled = enabled
        existing.exempt_owner_user_id = exempt_owner_user_id
        existing.email_domains = domains
        existing.updated_at = datetime.now(UTC)
        await session.flush()
        cfg = SsoConfig.from_row(existing)

    # Keep the fast-access denormalized columns on `orgs` in sync so
    # `core/tenancy.resolve_auth_org` returns current SSO gate values
    # without joining sso_configs on every request.
    from app.core.tenancy import set_sso_authz_for_org  # noqa: PLC0415

    await set_sso_authz_for_org(
        session,
        org_id=org_id,
        enabled=cfg.enabled,
        exempt_owner=cfg.exempt_owner_user_id,
    )
    return cfg


async def get_config(session: AsyncSession, *, org_id: UUID) -> SsoConfig | None:
    from sqlalchemy import select  # noqa: PLC0415

    row = (
        await session.execute(select(SsoConfigRow).where(SsoConfigRow.org_id == org_id))
    ).scalar_one_or_none()
    return SsoConfig.from_row(row) if row is not None else None


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
        except Exception as exc:
            # inside-span failure: SSO callback FastAPI span is active
            span = trace.get_current_span()
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, str(exc))
            log.exception("orgs.sso.verifier_failed")
            continue
        if out is not None:
            return out
    return None


# Register the core/saml production verifier at module load. Test stubs
# (`plugins/saml_test`) register their own verifier into the same list at
# boot; first non-None wins. When the production library can't load
# (missing libxmlsec1 in some local-dev environments) `_core_verify_assertion`
# returns None and the next verifier in the list gets a turn.
register_assertion_verifier(_core_verify_assertion)
