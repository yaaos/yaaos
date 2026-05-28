# core/saml

> Generic SAML SP primitives — keypair, assertion verification, metadata.

## Scope

- Owns: `is_available()`, `generate_sp_keypair()`, `verify_assertion()`, `parse_assertion()`.
- Does NOT own: `sso_configs` table (lives in `domain/orgs`). Pure protocol.

## Why / invariants

**`is_available()` guards `libxmlsec1`** — `python3-saml` binds to `libxmlsec1` at C-extension load time. Returns `False` in envs without the native library so non-prod stacks boot cleanly. Production Docker image installs `libxmlsec1` + `xmlsec1`.

**`verify_assertion` returns `None` on library-unavailable or parse failure** — `domain/orgs/sso` registers it into its assertion-verifier list; test stubs register a parallel verifier (first non-None wins).

**`generate_sp_keypair()`** — placeholder: random secret encrypted via `core/secrets` + `"POC-PLACEHOLDER-CERT"`. Real RSA keypair via `cryptography.hazmat` swaps in here without touching `domain/orgs/sso`.

