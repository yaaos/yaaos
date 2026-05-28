# core/saml

> Generic SAML SP primitives — keypair, assertion verification, metadata.

## Purpose

Single home for SAML SP mechanics. absorbed the `plugins/saml` adapter + the SP-keypair generator from `domain/orgs/sso` so the protocol bits live in one module that's free of domain awareness. `domain/orgs/sso` imports the verifier at module load and registers it into its assertion-verifier list; test stubs (`plugins/saml_test`) register their own verifier into the same list (first non-None wins).

## Public interface

- `is_available() -> bool` — True when `python3-saml` imports cleanly. Local-dev envs without `libxmlsec1` get False.
- `generate_sp_keypair() -> (bytes, str)` — placeholder: random secret encrypted via `core/secrets` + `"POC-PLACEHOLDER-CERT"` string. Real `cryptography.hazmat` RSA swaps in here without touching `domain/orgs/sso`.
- `verify_assertion(saml_response, idp_metadata_xml) -> dict | None` — the callable `domain/orgs/sso` registers. Returns None when the library can't load OR the parse fails.
- `parse_assertion(xml, settings_dict) -> dict` — lower-level. Raises `SamlNotAvailableError` when the library isn't importable.

## Module architecture

Stateless. `python3-saml` binds to `libxmlsec1` at C-extension load time; `is_available()` reports whether that succeeded so non-prod environments without the native library still boot cleanly (production deployments install `libxmlsec1` + `xmlsec1` in the docker image).

## Data owned

None — `sso_configs` lives in `domain/orgs`. `core/saml` is pure protocol.

## How it's tested

`test/test_availability.py` covers: `is_available()` doesn't raise; the verifier is registered into `domain/orgs/sso`'s list at import; unavailable-library returns None without crashing the dispatcher; `generate_sp_keypair()` round-trips through `core/secrets.decrypt`.
