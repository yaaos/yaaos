# core/saml

> Generic SAML SP primitives — keypair, assertion verification, metadata.

## Purpose

Wraps `python3-saml` so SAML mechanics live in one place. Phase 1c extracts the M02 SAML SP code from `plugins/saml` here so `domain/orgs/sso.py` no longer reaches into a plugin for protocol bits.

## Public interface

Planned (Phase 1c):

- `generate_sp_keypair() -> (private_key_pem, certificate_pem)`
- `verify_assertion(saml_response, idp_metadata_xml) -> AssertionResult`
- `generate_sp_metadata(entity_id, acs_url, sp_certificate) -> xml`

## Module architecture

Skeleton only. Phase 1c moves the existing implementation from `plugins/saml`.

## Data owned

None — `sso_configs` lives in `domain/orgs`.

## How it's tested

Tests land alongside the Phase 1c implementation: SP-keypair round-trip + signed-assertion verify + metadata generation.
