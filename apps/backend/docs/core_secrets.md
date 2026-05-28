# core/secrets

> Symmetric Fernet wrapper for at-rest application secrets.

## Scope

- Owns: `encrypt`, `decrypt`, `SecretsDecryptError`.
- Consumers: `core/identity/totp`, `domain/orgs/sso`, `core/byok`.
- Persisted ciphertext columns live in the calling modules (`user_totp_secrets.encrypted_secret`, `sso_configs.sp_private_key_encrypted`, `byok_keys.encrypted_value`).

## Why / invariants

**Master key resolution** — uses `settings.yaaos_totp_master_key` if set, else `settings.yaaos_encryption_key`. TOTP wants independent rotation from plugin-credential rotation; fallback lets dev/test run with a single key.

**`SecretsDecryptError`** wraps `cryptography.fernet.InvalidToken` so callers don't reach into `cryptography.fernet` directly.

