# core/secrets

> Symmetric Fernet wrapper for at-rest application secrets.

## Purpose

Single owner of Fernet construction. Wraps `cryptography.fernet.Fernet` so the master-key resolution + algorithm choice live in one place. Callers (`domain/identity/totp`, `domain/orgs/sso`, `core/byok`) call `encrypt`/`decrypt` instead of constructing their own cipher.

## Public interface

- `encrypt(plaintext: bytes | str) -> bytes` — Fernet ciphertext.
- `decrypt(ciphertext: bytes) -> bytes` — plaintext bytes.
- `SecretsDecryptError` — raised when ciphertext fails to decrypt with the configured key.

No HTTP routes.

## Module architecture

Master key resolution: `settings.yaaos_totp_master_key` if set, else `settings.yaaos_encryption_key`. Both are URL-safe base64 32-byte keys. The two-key story dates to M02 (TOTP wanted independent rotation from plugin-credential rotation); the fallback exists so dev/test stacks can run with a single key.

Decryption maps `cryptography.fernet.InvalidToken` to a domain-level `SecretsDecryptError` so callers don't reach into `cryptography.fernet` directly.

## Data owned

None — pure crypto primitive. Persisted ciphertext columns live in the calling modules (`user_totp_secrets.encrypted_secret`, `sso_configs.sp_private_key_encrypted`, `byok_keys.encrypted_value`, etc.).

## How it's tested

`test/test_roundtrip.py` covers: round-trip of bytes + str, and rejection of foreign ciphertext (sanity check that the wrong-key path raises `SecretsDecryptError`).
